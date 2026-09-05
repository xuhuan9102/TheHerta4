from ...blueprint.model import BluePrintModel
from ...common.submesh_model import SubMeshModel
from ...common.drawib_model import DrawIBModel
from dataclasses import dataclass,field
from ...common.global_config import GlobalConfig
from ...common.global_properties import GlobalProterties
from ...blueprint.export_helper import BlueprintExportHelper

from ...common.buffer_export_helper import BufferExportHelper
from ...common.draw_call_model import DrawCallModel
from ...common.global_key_count_helper import GlobalKeyCountHelper
from ...common.m_ini_helper import M_IniHelper
from ...common.m_ini_helper_gui import M_IniHelperGUI
from ...common.m_ini_builder import M_IniBuilder,M_IniSection, M_SectionType
from .export_helper import ExportHelper
from ...utils.json_utils import JsonUtils
from ...utils.timer_utils import TimerUtils

import bpy
import numpy
import os
import re
import shutil
import tempfile

# 与框架 cfg_ms_max_lod_level_count 一致；LodRemaps 每组件 4 个 LOD 槽位。
_EFMI_MAX_LOD_LEVEL_COUNT = 4
# 与 common.efmi_skeleton._CROSS_LOD_LAYOUT_VERSION 同步。旧版跨 LOD 编号
# （尤其 v9 投影/v12 压缩）不能进入当前单池运行时。
_EFMI_CROSS_LOD_LAYOUT_VERSION = 13

# --- I3 同 IB 折叠蒙皮兼容性阈值（导出不变量，独立于去重 match_tolerance）---
# 跨 LOD 对应账本（EFMILODCorrespondence）每行的 matrix_diff = 4x3 矩阵逐项
# 最大绝对差。same-IB 折叠复用同一 draw，L0/L1 版本的同一根骨骼只允许捕获
# 抖动级差异；实测错误对应（L1 骨骼匹配到 L0 另一根骨骼）差异达 272~449。
# 超过该阈值即拒绝静默折叠（raise），绝不产出“L1 显示套 L0 骨骼”的错误权重。
_SAME_IB_FOLD_MAX_MATRIX_DIFF = 1.0
_SAME_IB_FOLD_STRONG_GEOMETRY_MAX_MATRIX_DIFF = 16.0
_SAME_IB_FOLD_MAX_COMPONENT_SCORE = 1e-5
_SAME_IB_FOLD_MAX_CENTROID_DISTANCE = 1e-5

@dataclass
class ExportEFMI:

    blueprint_model:BluePrintModel

    submesh_model_list:list[SubMeshModel] = field(default_factory=list,init=False)
    drawib_model_list:list[DrawIBModel] = field(default_factory=list,init=False)

    def __post_init__(self):
        # EFMI 骨骼合并（复选框开启时）：为「需要生成但没有对象」的部件自动创建
        # 极限小三角面占位对象（对齐 ZZMI 机制；必须在组装 SubMeshModel 之前注入，
        # 占位部件才能照常进合并骨架：EntryPoint 照常触发、只画不可见小三角）
        self._efmi_stub_object_names = []
        if GlobalProterties.import_merged_vgmap():
            try:
                self._efmi_stub_object_names = self._ensure_stub_objects_for_missing_parts()
            except Exception as e:
                print(f"[EFMI骨骼合并] 占位小三角面创建失败（继续原流程）: {e}")
                self._efmi_stub_object_names = []

        self.submesh_model_list = ExportHelper.parse_submesh_model_list_from_blueprint_model(self.blueprint_model)
        # EFMI 直接复用已经解析好的 SubMeshModel，避免同一轮导出把几何解析做两遍。
        self.drawib_model_list = ExportHelper.parse_drawib_model_list_from_submesh_model_list(
            submesh_model_list=self.submesh_model_list,
            combine_ib=False,
        )
        print("SubMeshModel列表初始化完成，共有 " + str(len(self.submesh_model_list)) + " 个SubMeshModel")

        self.cross_ib_info_dict = self.blueprint_model.cross_ib_info_dict
        self.cross_ib_method_dict = self.blueprint_model.cross_ib_method_dict
        self.has_cross_ib = self.blueprint_model.has_cross_ib
        self.cross_ib_mapping_objects = self.blueprint_model.cross_ib_mapping_objects
        self.cross_ib_vb_condition_mapping = self.blueprint_model.cross_ib_vb_condition_mapping
        self.cross_ib_source_to_target_dict = self.blueprint_model.cross_ib_source_to_target_dict
        self.cross_ib_object_vb_condition = self.blueprint_model.cross_ib_object_vb_condition
        self.cross_ib_target_info = self.blueprint_model.cross_ib_target_info
        self.cross_ib_match_mode = self.blueprint_model.cross_ib_match_mode
        self.cross_ib_object_names = self.blueprint_model.cross_ib_object_names

        self.shader_replace_info_list = getattr(self.blueprint_model, "shader_replace_info_list", [])
        self.shader_replace_object_names = getattr(self.blueprint_model, "shader_replace_object_names", set())
        self.shader_replace_object_info_map = getattr(self.blueprint_model, "shader_replace_object_info_map", {})
        self.has_shader_replace = getattr(self.blueprint_model, "has_shader_replace", False)

        print(f"[CrossIB EFMI] 初始化: has_cross_ib={self.has_cross_ib}")
        print(f"[CrossIB EFMI] cross_ib_info_dict={self.cross_ib_info_dict}")
        print(f"[CrossIB EFMI] cross_ib_object_names={self.cross_ib_object_names}")
        print(f"[CrossIB EFMI] cross_ib_mapping_objects={self.cross_ib_mapping_objects}")

    # ------------------------------------------------------------------
    # 占位小三角面（骨骼合并模式：部件无对象时补齐，对齐 ZZMI 机制）
    # ------------------------------------------------------------------

    def _ensure_stub_objects_for_missing_parts(self) -> list[str]:
        """为「需要生成但没有对象」的部件创建极限小三角面占位对象。

        合并骨架模式下用户可自由 join/删改。占位规则（用户裁决 2026-11
        修正，与域前置同口径）：
        - **部分缺失的 DrawIB**：缺失组件直接补占位（其几何显然被同 DrawIB 的
          幸存对象接管）；
        - **整个 DrawIB 缺席**：按**统一顶点组（未去重）注册域**判定是否被
          并入保存对象——吸收证据只认「真·未注册槽」：现存对象实际使用
          （权重>0）的槽中，扣除**全工作区所有部件 json VGMap 的已注册槽并集**
          后剩下的槽，才可能是从被并入部件搬来的骨骼。统一骨架下任一组件
          引用另一组件的已注册槽是设计内合法状态（域前置放行域同口径），
          A 用它只是跨组件引用，不是吸收 B 的证据 → B 缺席时不生成占位
          （该 DrawIB 不进 mod，游戏内显示原版）。零引用同样不插桩。
        无反查数据（json 无 VGMap）的缺席 DrawIB 一律不插桩。

        多 LOD 语义（2026-08 实测定案）：LOD0 / LOD1 相互独立——每个 LOD 目录
        （LOD0/LOD1/...）有自己的 DrawIB-Component.json，各自按上述规则独立
        插桩；「被引用」判定只查**同 LOD** 现存对象的顶点组（跨 LOD 组 id 各自
        从 0 起会碰撞，混查会误判）。
        返回创建的对象名列表（export() 结束后清理）。
        """
        workspace_root = GlobalConfig.path_workspace_folder()
        ordered = getattr(self.blueprint_model, "ordered_draw_obj_data_model_list", None)
        if ordered is None:
            return []

        # 收集每个 LOD 目录（+ 根目录兜底）的 DrawIB-Component.json
        lod_component_maps: dict[str, dict] = {}
        if os.path.isdir(workspace_root):
            for entry in os.scandir(workspace_root):
                if not entry.is_dir():
                    continue
                if not re.match(r"^LOD\d+$", entry.name):
                    continue
                map_path = os.path.join(entry.path, "DrawIB-Component.json")
                if not os.path.isfile(map_path):
                    continue
                payload = JsonUtils.LoadFromFile(map_path)
                if isinstance(payload, dict) and payload:
                    lod_component_maps[entry.name] = payload
        root_map_path = os.path.join(workspace_root, "DrawIB-Component.json")
        if os.path.isfile(root_map_path):
            payload = JsonUtils.LoadFromFile(root_map_path)
            if isinstance(payload, dict) and payload:
                lod_component_maps.setdefault("", payload)
        if not lod_component_maps:
            return []

        # 现存对象按 LOD 分组（bare unique_str）
        present_by_lod: dict[str, set[str]] = {}
        for draw_call in ordered:
            try:
                unique_str = str(draw_call.get_workspace_unique_str() or "")
            except Exception:
                continue
            if not unique_str:
                continue
            lod_name = ""
            bare = unique_str
            if unique_str.upper().startswith("LOD") and "." in unique_str:
                dot_idx = unique_str.index(".")
                prefix = unique_str[:dot_idx]
                if prefix[3:].isdigit():
                    lod_name, bare = prefix, unique_str[dot_idx + 1:]
            present_by_lod.setdefault(lod_name, set()).add(bare)

        # 自愈：清掉上次导出异常残留的占位对象，避免被当成真实部件
        # （只认 EFMI_STUB 标记，不依赖对象名前缀——根目录部件 stub 无 LOD 前缀）
        for obj in list(bpy.data.objects):
            if obj.get("EFMI_STUB"):
                bpy.data.objects.remove(obj, do_unlink=True)

        used_group_ids_by_lod = None  # 惰性计算：首个全缺 DrawIB 需要判定时才算
        declared_vg_ids_by_lod = None  # 全工作区已注册槽并集（统一顶点组合法引用域，按 LOD）

        def _get_used_group_ids(lod_name: str) -> set[int]:
            nonlocal used_group_ids_by_lod
            if used_group_ids_by_lod is None:
                used_group_ids_by_lod = self._collect_used_group_ids_by_lod(ordered)
            return used_group_ids_by_lod.get(lod_name, set())

        def _get_declared_vg_ids(lod_name: str) -> set[int]:
            nonlocal declared_vg_ids_by_lod
            if declared_vg_ids_by_lod is None:
                declared_vg_ids_by_lod = self._collect_declared_vg_ids_by_lod()
            return declared_vg_ids_by_lod.get(lod_name, set())

        created = []
        for lod_name in sorted(lod_component_maps.keys()):
            component_map = lod_component_maps[lod_name]
            search_dir = os.path.join(workspace_root, lod_name) if lod_name else workspace_root
            present = present_by_lod.get(lod_name, set())
            lod_label = lod_name or "根目录"

            for draw_ib, comp_dict in component_map.items():
                members = sorted(str(v) for v in (comp_dict or {}).values())
                if not members:
                    continue

                if any(member in present for member in members):
                    # 部分缺失：缺失组件补占位
                    stub_members = [member for member in members if member not in present]
                else:
                    # 整个 DrawIB 缺席：判定「几何被并入现存对象」必须以
                    # **统一顶点组（未去重）注册域**为基线（用户裁决 2026-11）：
                    # 骨骼合并后全场共用一副骨架，任一组件引用另一组件的
                    # **已注册槽**是设计内合法状态（域前置放行域 =
                    # submesh_model._dualset_registered_slots_union 的
                    # 全工作区并集，注释同口径）。因此吸收证据只认「真·未注册槽」
                    # —— 现存对象 A 实际使用（权重>0）的槽中，扣除全工作区
                    # 全部 json VGMap 已注册槽后剩下的槽，才可能是从被并入
                    # 部件搬来的骨骼；矩阵相同的共享槽（A local 10 与 B local 20
                    # 去重合并到同一 canonical）必然同时注册在全工作区 json 里，
                    # A 用它只是「跨组件引用已注册槽」，不是吸收 B 的证据，
                    # B 缺席时不应为其生成占位（游戏保留原版）。
                    absorbed = self._is_drawib_absorbed(
                        draw_ib,
                        search_dir,
                        _get_used_group_ids(lod_name),
                        _get_declared_vg_ids(lod_name),
                    )

                    if absorbed:
                        stub_members = members
                        print(
                            f"[EFMI骨骼合并] DrawIB {draw_ib}（{lod_label}）没有对象，"
                            f"但其 VGMap 顶点组被其它模型引用（已被合并），全组件补占位小三角面"
                        )
                    else:
                        print(
                            f"[EFMI骨骼合并] DrawIB {draw_ib}（{lod_label}）无对象且顶点组未被引用，"
                            f"按用户意图不生成"
                        )
                        continue

                for member in stub_members:
                    if self._is_component_dedup_excluded(member, lod_name):
                        print(
                            f"[EFMI骨骼合并] 部件 {member}（{lod_label}）已标记 "
                            "VGMapDedupExcluded，按用户意图不生成占位（游戏保留原版绘制）"
                        )
                        continue
                    obj_name = self._create_stub_object(member, lod_name)
                    if obj_name:
                        ordered.append(DrawCallModel(obj_name=obj_name))
                        created.append(obj_name)
                        print(
                            f"[EFMI骨骼合并] 部件 {member}（{lod_label}）没有对应对象，"
                            f"已创建极限小三角面占位（游戏内不可见）"
                        )
        return created

    def _load_drawib_vg_values(self, draw_ib: str, search_dir: str) -> set[int]:
        """读取 DrawIB 全部组件写回的 VGMap 全局骨骼 id 集合（无数据返回空）。

        search_dir 为所属 LOD 的目录（LOD1 部件必须查 LOD1/，硬编码 LOD0 会漏）。
        """
        values = set()
        if not os.path.isdir(search_dir):
            return values
        for name in os.listdir(search_dir):
            if not name.startswith(draw_ib + "-"):
                continue
            submesh_dir = os.path.join(search_dir, name)
            if not os.path.isdir(submesh_dir):
                continue
            for type_dir in os.listdir(submesh_dir):
                if not type_dir.startswith("TYPE_"):
                    continue
                json_path = os.path.join(submesh_dir, type_dir, name + ".json")
                if not os.path.isfile(json_path):
                    continue
                payload = JsonUtils.LoadFromFile(json_path)
                vg_map = payload.get("VGMap") or {}
                for v in vg_map.values():
                    try:
                        values.add(int(v))
                    except (TypeError, ValueError):
                        continue
        return values

    def _is_drawib_absorbed(
        self,
        draw_ib: str,
        search_dir: str,
        used_group_ids: set[int],
        declared_vg_ids: set[int],
    ) -> bool:
        """按「统一顶点组（未去重）注册域」基线判定整个缺席的 DrawIB 是否已并入现存对象。

        吸收的证据 = 现存对象实际使用的槽中，存在**未由全工作区任何组件 json
        VGMap 声明**的槽（真·未注册槽——统一骨架下必然不是合法跨组件引用，
        只能来自被并入的部件）；该未注册槽落在缺席 DrawIB 的 VGMap 值域内
        即判定吸收。矩阵相同的共享槽（去重合并到同一 canonical）必然同时注册
        在全工作区 json 里，跨组件引用已注册槽是设计内合法状态，不计入吸收证据。
        """
        vg_values = self._load_drawib_vg_values(draw_ib, search_dir)
        if not vg_values:
            return False
        foreign_used = set(used_group_ids) - set(declared_vg_ids)
        return bool(vg_values & foreign_used)

    def _collect_declared_vg_ids_by_lod(self, ordered=None) -> dict[str, set[int]]:
        """收集全工作区各部件的 json VGMap 已注册槽并集，按 LOD 分组。

        语义（用户裁决 2026-11）：统一顶点组（未去重）模式下全场共用一副骨架，
        任一组件引用另一组件的**已注册槽**是设计内合法状态（与域前置的放行域
        submesh_model._dualset_registered_slots_union 同口径）。占位判定的
        基线因此必须是「全工作区全部 json VGMap 的并集」，而不是「导出集合内
        各部件的声明」——后者会把缺席部件已注册、却被现存对象正常引用的槽
        误判成「外来槽」，触发错误的吸收占位（实盘案例 GDZGF：a4bb34f9 网格
        267 槽全部落在全工作区 621 槽注册域内，仍被误判吸收 ddc92b8b）。

        生成的 set 不包括只读全局缓存之外的数据：全部实时扫描，跟随 json
        变更（重新抽取 / VGMap 重算）自动生效，无需清缓存。
        """
        declared: dict[str, set[int]] = {}
        workspace_root = GlobalConfig.path_workspace_folder()
        if not os.path.isdir(workspace_root):
            return declared

        # 扫描根目录 + 所有 LOD* 目录下每个部件的 TYPE_* 子目录 json
        scan_roots = [""]
        for entry in os.scandir(workspace_root):
            if entry.is_dir() and re.match(r"^LOD\d+$", entry.name):
                scan_roots.append(entry.name)

        for lod_name in scan_roots:
            base = os.path.join(workspace_root, lod_name) if lod_name else workspace_root
            bucket = declared.setdefault(lod_name, set())
            try:
                entries = list(os.scandir(base))
            except OSError:
                continue
            for sub in entries:
                if not sub.is_dir():
                    continue
                if sub.name.startswith("DedupedTextures"):
                    continue
                submesh_dir = os.path.join(base, sub.name)
                for type_dir in sorted(os.listdir(submesh_dir)):
                    if not type_dir.startswith("TYPE_"):
                        continue
                    json_path = os.path.join(
                        submesh_dir, type_dir, sub.name + ".json"
                    )
                    if not os.path.isfile(json_path):
                        continue
                    payload = JsonUtils.LoadFromFile(json_path)
                    vg_map = payload.get("VGMap") or {}
                    for raw in vg_map.values():
                        try:
                            bucket.add(int(raw))
                        except (TypeError, ValueError):
                            continue
        return declared

    def _locate_component_json(
        self, workspace_root: str, lod_name: str, bare: str
    ) -> str:
        base = os.path.join(workspace_root, lod_name) if lod_name else workspace_root
        submesh_dir = os.path.join(base, bare)
        if not os.path.isdir(submesh_dir):
            return ""
        for type_dir in sorted(os.listdir(submesh_dir)):
            if not type_dir.startswith("TYPE_"):
                continue
            json_path = os.path.join(submesh_dir, type_dir, bare + ".json")
            if os.path.isfile(json_path):
                return json_path
        return ""

    def _collect_used_group_ids_by_lod(self, ordered) -> dict[str, set[int]]:
        """收集蓝图内全部对象实际引用（权重>0）的全局顶点组 id，按 LOD 分组。

        跨 LOD 组 id 各自从 0 起（命名空间独立），判定某 LOD 的缺席 DrawIB
        是否被吸收时必须只用**同 LOD** 对象的组 id，混查会因编号碰撞误判。
        全局 id 取数字顶点组名称，不取 Blender 内部 group index；替换模型删除
        空组后内部下标会压缩，但统一顶点组名称仍保持全局骨骼编号。
        """
        used: dict[str, set[int]] = {}
        for draw_call in ordered:
            try:
                obj_name = draw_call.get_blender_obj_name()
                unique_str = str(draw_call.get_workspace_unique_str() or "")
            except Exception:
                continue
            lod_name = ""
            if unique_str.upper().startswith("LOD") and "." in unique_str:
                dot_idx = unique_str.index(".")
                prefix = unique_str[:dot_idx]
                if prefix[3:].isdigit():
                    lod_name = prefix
            obj = bpy.data.objects.get(obj_name) if obj_name else None
            if obj is not None and obj.get("EFMI_STUB"):
                # 本轮先创建的占位固定带组 0；它不是用户模型，不能反过来成为
                # 后续缺席 Component “已并入其它对象”的关系证据。
                continue
            mesh = getattr(obj, "data", None) if obj is not None else None
            vertices = getattr(mesh, "vertices", None)
            vertex_groups = getattr(obj, "vertex_groups", None) if obj is not None else None
            if vertices is None or vertex_groups is None:
                continue
            bucket = used.setdefault(lod_name, set())
            for vertex in vertices:
                for group_elem in vertex.groups:
                    if group_elem.weight <= 0:
                        continue
                    try:
                        group_name = vertex_groups[group_elem.group].name
                        global_group_id = int(str(group_name).strip())
                    except (IndexError, KeyError, TypeError, ValueError):
                        continue
                    if global_group_id >= 0:
                        bucket.add(global_group_id)
        return used

    def _is_component_dedup_excluded(self, bare_unique_str: str, lod_name: str) -> bool:
        """部件是否被用户显式排除（json 标记 VGMapDedupExcluded=True）。

        显式排除 = 用户意图“完全不出现在 mod 里”：跳过占位小三角面生成，
        游戏侧保留原版绘制。与占位的 `absorbed`（几何被合并进其它对象）
        语义正交——合并场景下的缺失部件仍须占位抑制重影，排除部件则相反。
        """
        base = GlobalConfig.path_workspace_folder()
        search_roots = [os.path.join(base, lod_name), base] if lod_name else [base]
        for root in search_roots:
            submesh_dir = os.path.join(root, bare_unique_str)
            if not os.path.isdir(submesh_dir):
                continue
            for type_dir in sorted(os.listdir(submesh_dir)):
                if not type_dir.startswith("TYPE_"):
                    continue
                json_path = os.path.join(submesh_dir, type_dir, bare_unique_str + ".json")
                if not os.path.isfile(json_path):
                    continue
                payload = JsonUtils.LoadFromFile(json_path)
                if bool(payload.get("VGMapDedupExcluded")):
                    return True
                return False
        return False

    def _create_stub_object(self, bare_unique_str: str, lod_name: str = "LOD0") -> str:
        """创建占位对象：3 顶点 1 三角面（1e-6 尺度），权重挂在部件的已注册槽。

        对象名 = <LOD>.<bare>（ObjectPrefixHelper 可解析出带 LOD 前缀的
        workspace unique_str，保证 stub 部件从自己 LOD 的 json 读取骨骼元数据）。

        权重组名必须是**已注册槽**（json VGMap 值）：合并骨架模式下局部命名空间
        的组 "0" 不在全工作区注册域内，EFMI 双套域前置会拦截「被引用而缺席 →
        补占位 → 占位权重组名不落注册域」的导出；无反查数据（json 无 VGMap，
        局部命名空间部件）保持 "0" 与旧行为一致。
        """
        workspace_unique_str = (
            f"{lod_name}.{bare_unique_str}" if lod_name else bare_unique_str
        )

        mesh = bpy.data.meshes.new(name="EFMI_STUB_MESH_" + workspace_unique_str)
        mesh.from_pydata(
            [(0.0, 0.0, 0.0), (1e-6, 0.0, 0.0), (0.0, 1e-6, 0.0)],
            [],
            [(0, 1, 2)],
        )
        mesh.update()

        obj = bpy.data.objects.new(name=workspace_unique_str, object_data=mesh)
        obj["EFMI_STUB"] = 1
        obj["3DMigoto:WorkspaceUniqueStr"] = workspace_unique_str
        slot_group_name = self._resolve_stub_registered_slot(
            bare_unique_str, lod_name
        )
        vertex_group = obj.vertex_groups.new(name=slot_group_name)
        vertex_group.add([0, 1, 2], 1.0, 'REPLACE')

        try:
            bpy.context.collection.objects.link(obj)
        except Exception:
            bpy.context.scene.collection.objects.link(obj)
        return obj.name

    def _resolve_stub_registered_slot(self, bare_unique_str: str, lod_name: str) -> str:
        """解析占位三角的权重槽：合并骨架部件取 json VGMap 的第一个已注册槽。

        缺失部件的 VGMap 引用槽必然在全工作区注册域内（域前置 = 本组件 VGMap
        引用槽 ∪ 全工作区并集），占位权重组名落在域内即可通过双套更名/域前置；
        随后 build_per_mesh_identity_map 会把它映射到本组件身份段，经合并预处理
        排序后写盘的是合法全局骨骼 id。json 无 VGMap（局部命名空间/无反查数据）
        时返回 "0"（与旧行为一致，不参与双套路径）。
        """
        base = GlobalConfig.path_workspace_folder()
        search_roots = [os.path.join(base, lod_name), base] if lod_name else [base]
        for root in search_roots:
            submesh_dir = os.path.join(root, bare_unique_str)
            if not os.path.isdir(submesh_dir):
                continue
            for type_dir in sorted(os.listdir(submesh_dir)):
                if not type_dir.startswith("TYPE_"):
                    continue
                json_path = os.path.join(submesh_dir, type_dir, bare_unique_str + ".json")
                if not os.path.isfile(json_path):
                    continue
                payload = JsonUtils.LoadFromFile(json_path)
                vg_map = payload.get("VGMap") or {}
                for raw in vg_map.values():
                    try:
                        slot = int(raw)
                    except (TypeError, ValueError):
                        continue
                    if slot >= 0:
                        return str(slot)
                return "0"
        return "0"

    def _cleanup_stub_objects(self):
        """导出结束后移除占位对象（含 mesh 数据）。"""
        for obj_name in self._efmi_stub_object_names:
            obj = bpy.data.objects.get(obj_name)
            if obj is None:
                continue
            mesh = obj.data
            bpy.data.objects.remove(obj, do_unlink=True)
            if mesh is not None and mesh.users == 0:
                bpy.data.meshes.remove(mesh)
        if self._efmi_stub_object_names:
            print(f"[EFMI骨骼合并] 已清理 {len(self._efmi_stub_object_names)} 个占位小三角面对象")
        self._efmi_stub_object_names = []

    def generate_buffer_files(self):
        # 合并骨架部件（含 same-IB 槽位重定向）必须在本方法开始时已收集：
        # 导出可能被按阶段调用（仅缓冲/仅 INI），不能依赖 export() 的预热。
        self.prepare_merged_skeleton()

        buf_output_folder = GlobalConfig.path_generatemod_buffer_folder()
        output_folder = os.path.abspath(buf_output_folder)
        output_parent = os.path.dirname(output_folder)
        os.makedirs(output_parent, exist_ok=True)
        staging_folder = tempfile.mkdtemp(
            prefix=f".{os.path.basename(output_folder) or 'efmi-buffers'}.stage-",
            dir=output_parent,
        )
        try:
            self._write_buffer_files_to_folder(staging_folder)
            removed_count = self._publish_staged_buffer_bundle(
                staging_folder,
                output_folder,
            )
            if removed_count:
                print(f"[EFMI] 已替换 {removed_count} 个上次导出的 .buf")
        finally:
            shutil.rmtree(staging_folder, ignore_errors=True)

    @staticmethod
    def _publish_staged_buffer_bundle(staging_folder: str, output_folder: str) -> int:
        """Publish a complete buffer set with rollback on any replacement failure."""
        staging_folder = os.path.abspath(staging_folder)
        output_folder = os.path.abspath(output_folder)
        output_parent = os.path.dirname(output_folder)
        os.makedirs(output_folder, exist_ok=True)

        staged_names = sorted(
            name
            for name in os.listdir(staging_folder)
            if name.endswith(".buf") and os.path.isfile(os.path.join(staging_folder, name))
        )
        previous_names = sorted(
            name
            for name in os.listdir(output_folder)
            if name.endswith(".buf") and os.path.isfile(os.path.join(output_folder, name))
        )
        backup_folder = tempfile.mkdtemp(
            prefix=f".{os.path.basename(output_folder) or 'efmi-buffers'}.backup-",
            dir=output_parent,
        )
        moved_previous = []
        published = []
        try:
            for name in previous_names:
                os.replace(
                    os.path.join(output_folder, name),
                    os.path.join(backup_folder, name),
                )
                moved_previous.append(name)
            for name in staged_names:
                os.replace(
                    os.path.join(staging_folder, name),
                    os.path.join(output_folder, name),
                )
                published.append(name)
        except Exception:
            # 回滚仅触及本次已经发布的文件和刚移入备份的旧 .buf。
            for name in reversed(published):
                published_path = os.path.join(output_folder, name)
                try:
                    if os.path.exists(published_path):
                        os.remove(published_path)
                except OSError:
                    pass
            for name in reversed(moved_previous):
                backup_path = os.path.join(backup_folder, name)
                try:
                    if os.path.exists(backup_path):
                        os.replace(backup_path, os.path.join(output_folder, name))
                except OSError:
                    pass
            raise
        finally:
            shutil.rmtree(backup_folder, ignore_errors=True)
        return len(previous_names)

    def _write_buffer_files_to_folder(self, buf_output_folder: str):
        # FC-4（C9）：折叠死段集合与写盘断言所需的折叠别名目标。
        folded_dead_segments: list[dict] = getattr(
            self, "_efmi_folded_dead_segments", []
        ) or []
        fold_alias_targets: dict[int, int] = getattr(
            self, "_efmi_fold_alias_targets", {}
        ) or {}
        merged_draw_entries: dict = getattr(
            self, "_efmi_merged_draw_entries", {}
        ) or {}
        for submesh_model in self.submesh_model_list:
            print("ExportEFMI: 导出SubMeshModel，Unique标识: " + submesh_model.unique_str)

            ib_name = getattr(submesh_model, "workspace_unique_str", "") or submesh_model.unique_str
            ib_filename = ib_name + "-Index.buf"
            ib_filepath = os.path.join(buf_output_folder, ib_filename)
            BufferExportHelper.write_buf_ib_r32_uint(submesh_model.ib, ib_filepath)

            for category, category_buf in submesh_model.category_buffer_dict.items():
                category_buf_filename = submesh_model.unique_str + "-" + category + ".buf"
                category_buf_filepath = os.path.join(buf_output_folder, category_buf_filename)
                output_buf = category_buf
                bone_aliases = getattr(
                    self, "_efmi_merged_skeleton_bone_aliases", {}
                )
                if bone_aliases:
                    output_buf, remapped_count = self._remap_blendindices_category_buffer(
                        category_buf,
                        category,
                        submesh_model.d3d11_game_type,
                        bone_aliases,
                    )
                    if remapped_count:
                        print(
                            f"[EFMI骨骼合并] {submesh_model.unique_str}: "
                            f"已重定向 {remapped_count} 个 same-IB 跨 LOD 骨骼索引"
                        )
                # FC-2 写盘域断言（t3 设计 §4，替代旧注释 :459-464 的「不按段拦截」）：
                # 对**产物缓冲**（无论更名/折叠/I2 是否执行，与源头无关）断言——
                # 写盘 BLENDINDICES ⊆ 自属声明段 ∪ 折叠别名目标段；越段即
                # RuntimeError 中止导出（fail-closed，不静默写盘）。前置条件 =
                # EFMI 合并候选（dualset_eligible：EFMI + VGMap 算法版本>0 +
                # 元数据有效 + GPU_PreSkinning），非 EFMI/ZZMI/CPU 路径不激活。
                # 为何不是旧「按 LOD 声明段拦截」：投影/快速局部导出允许保留部件
                # 引用其它部件的池槽位（canonical 借位），但必须经更名消化成
                # 自属身份；此处断言的是更名后身份域，不是原始槽位域。
                if (
                    str(getattr(GlobalConfig, "logic_name", "") or "") == "EFMI"
                    and int(getattr(submesh_model, "vg_map_algorithm_version", 0) or 0) > 0
                    and bool(
                        getattr(submesh_model, "merged_skeleton_metadata_valid", True)
                    )
                    and bool(
                        getattr(
                            getattr(submesh_model, "d3d11_game_type", None),
                            "GPU_PreSkinning",
                            False,
                        )
                    )
                ):
                    self._assert_fc2_fc4_written_blendindices(
                        submesh_model=submesh_model,
                        category=category,
                        output_buf=output_buf,
                        folded_dead_segments=folded_dead_segments,
                        fold_alias_targets=fold_alias_targets,
                        bound=submesh_model.unique_str in merged_draw_entries,
                    )
                with open(category_buf_filepath, 'wb') as f:
                    output_buf.tofile(f)

        # 合并骨架 full→lod BlendRemap（R16_UINT，长度 = 基准部件 vg_count）。
        if getattr(self, "has_merged_skeleton", False):
            for part in getattr(self, "merged_skeleton_components", []) or []:
                for draw in part.get("draws", []):
                    remap = draw.get("remap")
                    if not remap:
                        continue
                    arr = numpy.array(remap, dtype=numpy.uint64)
                    if arr.size and arr.max() > 65535:
                        raise RuntimeError(
                            f"[EFMI骨骼合并] {draw.get('unique_str', '?')} 的 BlendRemap "
                            f"含超过 uint16 上限的局部 id（{int(arr.max())}），无法以 R16_UINT 承载"
                        )
                    remap_buf_filename = draw["unique_str"] + "-BlendRemap.buf"
                    remap_buf_path = os.path.join(buf_output_folder, remap_buf_filename)
                    numpy.array(remap, dtype=numpy.uint16).tofile(remap_buf_path)
                    print(
                        f"[EFMI骨骼合并] 已导出 BlendRemap: {remap_buf_filename} "
                        f"({len(remap)} 项)"
                    )

    def _get_submesh_ib_key(self, submesh_model):
        if self.cross_ib_match_mode == 'INDEX_COUNT':
            return f"indexcount_{submesh_model.match_index_count}"
        else:
            return f"{submesh_model.match_draw_ib}_{submesh_model.match_first_index}"

    @staticmethod
    def _extract_active_blendindices_values(category_buf, category_name, d3d11_game_type):
        """从打包类别缓冲提取**权重 > 0** 的 BLENDINDICES 值（R16 整数系；无 bpy）。

        关键语义：Blender 侧打包（vertexgroup_utils.get_blendweights_blendindices_
        v4_fast）对未占用通道写 **index=0 + weight=0.0**——0 通道运行时被权重
        0 消去（mesh_create_helper 的 valid_mask 同口径），不会参与蒙皮。因此
        FC-2 写盘断言**只能断言权重 > 0 的通道**：零权重通道的索引（0 哨兵等）
        运行时惰性，断言其段归属会造成全量误报（所有 LOD1 部件的段都不含槽 0）。

        与 ``_remap_blendindices_category_buffer`` 同构：按 CategoryStrideDict
        切行、逐元素按 AlignedByteOffset 定位；非 R16 整数 BLENDINDICES =
        未升宽的合并形态，抛 RuntimeError（合并候选必然已 widen 到 R16 系）。
        权重格式支持 UNORM/SNORM/UINT（原始 ≠ 0 即活跃）与 FLOAT（> 0.0）；
        **无 BLENDWEIGHT 元素**（刚性/单骨 BI-only 布局，如 GPU_P12_N4_T8_BI4_）
        按「仅通道 0 活跃」处理（F1/t5：无权重即无混合，运行时只读通道 0，
        未占用通道的 index=0 哨兵不是活跃引用）。
        返回 numpy int64 一维数组（无活跃通道/空缓冲返回空数组）。
        """
        raw = numpy.ascontiguousarray(category_buf)
        if raw.dtype != numpy.uint8:
            raw = raw.view(numpy.uint8)
        raw = raw.reshape(-1)
        stride = int(
            getattr(d3d11_game_type, "CategoryStrideDict", {}).get(
                category_name, 0
            )
            or 0
        )
        if stride <= 0 or raw.size == 0:
            return numpy.empty((0,), dtype=numpy.int64)
        if raw.size % stride != 0:
            raise RuntimeError(
                f"[EFMI骨骼合并/FC-2] {category_name} 缓冲大小 {raw.size} "
                f"不能被 stride {stride} 整除"
            )
        rows = raw.reshape(-1, stride)
        weight_positions: dict[int, tuple[int, int, str]] = {}
        index_positions: dict[int, tuple[int, int, str]] = {}
        category_offset = 0
        for element in getattr(d3d11_game_type, "D3D11ElementList", []):
            if str(getattr(element, "Category", "") or "") != category_name:
                continue
            width = int(getattr(element, "ByteWidth", 0) or 0)
            semantic = str(getattr(element, "SemanticName", "") or "").upper()
            semantic_index = int(getattr(element, "SemanticIndex", 0) or 0)
            element_format = str(getattr(element, "Format", "") or "").upper()
            record = (category_offset, width, element_format)
            if semantic == "BLENDINDICES":
                index_positions[semantic_index] = record
            elif semantic == "BLENDWEIGHT" or semantic == "BLENDWEIGHTS":
                weight_positions[semantic_index] = record
            category_offset += width

        collected = []
        for semantic_index, (index_offset, index_width, index_format) in sorted(
            index_positions.items()
        ):
            if not (
                index_format.startswith("R16")
                and index_format.endswith(("_UINT", "_SINT"))
                and index_width % 2 == 0
            ):
                raise RuntimeError(
                    "[EFMI骨骼合并/FC-2] 写盘域断言要求 R16 整数 "
                    f"BLENDINDICES，实际为 {index_format}（未执行 widen 升宽的"
                    "合并候选不可断言，中止导出）"
                )
            index_field = numpy.ascontiguousarray(
                rows[:, index_offset:index_offset + index_width]
            )
            index_values = index_field.view("<u2").reshape(len(rows), index_width // 2)
            # 权重掩码：同 SemanticIndex 的 BLENDWEIGHT，逐 (顶点,通道) 判定
            # 权重 > 0（零权重通道的索引运行时惰性——打包器对未占用通道写
            # index=0 + weight=0，不可断言其段归属）。
            weight_record = weight_positions.get(semantic_index)
            if weight_record is None:
                # F1（t5 修复）：无 BLENDWEIGHT 元素 = 刚性/单骨（BI-only）布局
                # （如 GPU_P12_N4_T8_BI4_）。打包器对未占用通道写 index=0 哨兵
                # 且**没有权重字节可消去**——运行时无权重即无混合，只读通道 0
                # （单骨刚性），通道 1-3 的哨兵 0 不是活跃骨骼引用。故只把通道 0
                # 视为活跃通道：哨兵 0 不得计为越段引用（否则 0000 刚性部件的
                # 合法合并导出被自身断言拦死）；通道 0 为 0 时（绑定骨骼 0 的
                # 部件）仍按真实引用断言。
                mask = numpy.zeros(index_values.shape, dtype=bool)
                if index_values.shape[1] >= 1:
                    mask[:, 0] = True
            else:
                weight_offset, weight_width, weight_format = weight_record
                weight_field = numpy.ascontiguousarray(
                    rows[:, weight_offset:weight_offset + weight_width]
                )
                mask = ExportEFMI._positive_weight_mask(
                    weight_field, weight_width, weight_format, index_values.shape[1]
                )
            active = index_values[mask]
            if active.size == 0:
                continue
            collected.append(active.reshape(-1).astype(numpy.int64))
        if not collected:
            return numpy.empty((0,), dtype=numpy.int64)
        return numpy.concatenate(collected)

    @staticmethod
    def _positive_weight_mask(weight_field, weight_width: int, weight_format: str,
                              channels: int):
        """权重元素 > 0 的 (n, channels) 逐通道掩码（BLENDWEIGHT 同口径）。

        UNORM/SNORM/UINT（整数位型）：原始 ≠ 0 即活跃（精度无损，0 值只来自
        未占用通道）；FLOAT：解码后 > 0.0。通道数按格式串推导
        （R8G8B8A8 → 4×u8；R16G16B16A16 → 4×u16；R8 → 1×u8）。权重字段通道数
        与索引字段不一致时：权重覆盖到的通道按权重判定，其余视为不活跃
        （运行时同样无权重惰性）。
        """
        if weight_width <= 0 or channels <= 0:
            return numpy.zeros((len(weight_field), channels), dtype=bool)
        component_bits = []
        for token in re.findall(r"[RGBA](\d+)", str(weight_format or "")):
            try:
                component_bits.append(int(token))
            except (TypeError, ValueError):
                continue
        if not component_bits:
            raise RuntimeError(
                f"[EFMI骨骼合并/FC-2] 无法解析 BLENDWEIGHT 格式 {weight_format}"
            )
        comp_bytes = component_bits[0] // 8
        declared_channels = max(1, weight_width // max(comp_bytes, 1))
        if weight_width % max(comp_bytes, 1) != 0:
            raise RuntimeError(
                f"[EFMI骨骼合并/FC-2] BLENDWEIGHT 宽度 {weight_width} 与格式 "
                f"{weight_format} 的组件位宽不匹配"
            )
        if weight_format.endswith("_FLOAT"):
            dtype = numpy.float32 if comp_bytes == 4 else (
                numpy.float16 if comp_bytes == 2 else numpy.float32
            )
            values = weight_field.view(dtype).reshape(len(weight_field), -1)
            mask = values > 0.0
        elif "SNORM" in weight_format:
            dtype = numpy.int32 if comp_bytes == 4 else (
                numpy.int16 if comp_bytes == 2 else numpy.int8
            )
            values = weight_field.view(dtype).reshape(len(weight_field), -1)
            mask = values != 0
        else:
            # UNORM / UINT / 其它整数位型：原始非零即活跃
            dtype = numpy.uint32 if comp_bytes == 4 else (
                numpy.uint16 if comp_bytes == 2 else numpy.uint8
            )
            values = weight_field.view(dtype).reshape(len(weight_field), -1)
            mask = values != 0
        weight_channels = mask.shape[1]
        if weight_channels == channels:
            return mask
        if weight_channels == 1:
            return numpy.repeat(mask, channels, axis=1)
        aligned = numpy.zeros((len(weight_field), channels), dtype=bool)
        covered = min(weight_channels, channels)
        aligned[:, :covered] = mask[:, :covered]
        return aligned

    def _assert_fc2_fc4_written_blendindices(
        self,
        submesh_model,
        category: str,
        output_buf,
        folded_dead_segments: list[dict],
        fold_alias_targets: dict[int, int],
        bound: bool,
    ) -> None:
        """FC-2 写盘域断言 + FC-4 死段零写入断言（C4/C9，产物级硬闸）。

        - FC-2：写盘 BLENDINDICES ⊆ ``[vg_offset, vg_offset+vg_count) ∪
          折叠别名目标段 ∪ 全工作区已注册槽并集``（t18，用户拍板，接 t17 终验
          铁证），违反 → RuntimeError（比对的是**最终写盘缓冲**，与更名/折叠/
          I2 是否执行无关——旧版/绕开更名路径直写槽位在此必被拦；跨组件已注册
          槽 = 单池全局骨骼身份，合法放行；json 元数据缺失时并集为空 → 退化为
          自属段 ∪ 折叠目标 + 「元数据缺失」明确消息，fail-closed 不弱化）。
        - FC-4：绑定网格（有 merged draw entry / 独立入口）的写盘索引不得命中
          任何折叠死段（t2 三死段）。折叠部件自身（未绑定、无独立 EntryPoint、
          INI 不注册其段）的缓冲是未绑定死文件，其值域落在自己声明段内属
          FC-2 合法域，FC-4 豁免并只在日志提示（供 t5 §7.3-3 对照说明）。
        """
        from ...common.efmi_skeleton import EFMIBoneMapBuilder
        from ...common.submesh_model import SubMeshModel as _SubMeshModel

        unique_str = str(getattr(submesh_model, "unique_str", "") or "")
        vg_offset = int(getattr(submesh_model, "vg_offset", 0) or 0)
        vg_count = int(getattr(submesh_model, "vg_count", 0) or 0)
        segment = (vg_offset, vg_offset + vg_count)
        indices = self._extract_active_blendindices_values(
            output_buf, category, submesh_model.d3d11_game_type
        )
        if indices.size == 0:
            return
        targets = set(int(v) for v in fold_alias_targets.values())
        # t18：全工作区已注册槽并集（复用 submesh_model._dualset_registered_slots_union，
        # 不复制实现；无法解析工作区时 None = 不启用并集 = 原单组件口径）。
        registered_extra = None
        try:
            _pf = getattr(GlobalConfig, "path_workspace_folder", None)
            _workspace_root = str(_pf() or "") if callable(_pf) else ""
        except Exception:
            _workspace_root = ""
        if _workspace_root and os.path.isdir(_workspace_root):
            try:
                registered_extra = _SubMeshModel._dualset_registered_slots_union(
                    _workspace_root
                )
            except Exception:
                registered_extra = None
        if registered_extra is None:
            # 未启用并集（工作区不可解析）：原单组件口径（验证器原消息）。
            EFMIBoneMapBuilder.validate_export_indices_in_segment(
                segment, targets, indices, component_label=unique_str
            )
        else:
            # 并集口径：先做集合成员判定（含两因区分），再把剔除并集成员后的
            # 索引交给原验证器做自属段/折叠目标结构断言（验证器本口径不变）。
            import numpy as _np
            in_segment = (
                (indices >= segment[0]) & (indices < segment[1])
            ).astype(bool)
            in_targets_arr = _np.zeros(indices.size, dtype=bool)
            for target in targets:
                in_targets_arr |= indices == target
            in_union_arr = _np.zeros(indices.size, dtype=bool)
            for slot in registered_extra:
                in_union_arr |= indices == slot
            bad_mask = ~(in_segment | in_targets_arr | in_union_arr)
            if bad_mask.any():
                bad_values = indices[bad_mask]
                preview = sorted({int(v) for v in bad_values})[:20]
                suffix = "…" if len(preview) == 20 and len(set(bad_values.tolist())) > 20 else ""
                if not registered_extra:
                    # 元数据缺失/被清（t15 场景）：与「未注册槽」原因区分
                    #（fail-closed 保持）。
                    raise RuntimeError(
                        f"[EFMI双套导出/FC-2] {unique_str} 写盘 BLENDINDICES "
                        f"{preview}{suffix}（共 {len(bad_values)} 项）无法判定槽位归属："
                        "全工作区已注册槽并集为空（骨骼合并元数据缺失/被清除，或 json "
                        "被外部进程改写）。产物将引用运行时未注册/未写入的骨骼槽位，"
                        "中止导出（fail-closed，不静默写盘）。请检查工作区 json 或按"
                        "当前工作区 json 重新导入该角色（含合并顶点组开关）后重试"
                    )
                raise RuntimeError(
                    f"[EFMI双套导出/FC-2] {unique_str} 写盘 BLENDINDICES 越出"
                    f"运行时身份域 [{segment[0]},{segment[1]}) ∪ 折叠目标 "
                    f"{sorted(targets)[:10]} ∪ 全工作区已注册槽 {len(registered_extra)} 项，"
                    f"非法引用 {preview}{suffix}（共 {len(bad_values)} 项）。产物将"
                    "引用未注册/段隙/越界槽位，中止导出（fail-closed，不静默写盘）。"
                    "请重新执行骨骼合并反查/重新导入该角色后重试"
                )
            EFMIBoneMapBuilder.validate_export_indices_in_segment(
                segment, targets, indices[~in_union_arr],
                component_label=unique_str,
            )
        # FC-4：绑定网格不得命中折叠死段（0000 即 [717,729)∪[817,827)∪[903,962)）。
        if not bound:
            return
        if folded_dead_segments:
            dead_mask = numpy.zeros(indices.size, dtype=bool)
            for item in folded_dead_segments:
                dead_start, dead_end = int(item["segment"][0]), int(item["segment"][1])
                dead_mask |= (indices >= dead_start) & (indices < dead_end)
            if dead_mask.any():
                hits = sorted({int(v) for v in indices[dead_mask]})[:20]
                raise RuntimeError(
                    f"[EFMI骨骼合并/FC-4] {unique_str} 写盘 BLENDINDICES 命中"
                    f"被折叠 LOD 家族死段: {hits}（这些槽位运行时无人写入、"
                    "INI 不注册）。产物将引用不存在骨骼，中止导出（fail-closed）。"
                    "请重新执行骨骼合并反查/重新导入该角色"
                )
        else:
            print(
                f"[EFMI骨骼合并/FC-4] {unique_str} 死段槽位零写入（无折叠家族）"
            )

    def _append_drawindexed_instanced_with_shader_replace(self, section, drawcall_list, draw_offset_dict):
        """将 drawcall 列表写入 section，对着色器替换物体使用 run 逻辑替代 instanced 绘制。"""
        if not self.has_shader_replace:
            for drawindexed_str in M_IniHelper.get_drawindexed_instanced_str_list(
                drawcall_list, obj_name_draw_offset_dict=draw_offset_dict,
            ):
                section.append(drawindexed_str)
            return

        resolved_drawcalls = [
            (
                drawcall,
                M_IniHelper.get_draw_call_shader_replace_info_list(
                    drawcall,
                    shader_replace_object_names=self.shader_replace_object_names,
                    shader_replace_object_info_map=self.shader_replace_object_info_map,
                    shader_replace_info_list=self.shader_replace_info_list,
                ),
            )
            for drawcall in drawcall_list
        ]
        for dc, obj_infos in resolved_drawcalls:
            if not obj_infos:
                for drawindexed_str in M_IniHelper.get_drawindexed_instanced_str_list(
                    [dc],
                    obj_name_draw_offset_dict=draw_offset_dict,
                ):
                    section.append(drawindexed_str)
                continue

            draw_offset = dc.index_offset
            if draw_offset_dict:
                draw_offset = draw_offset_dict.get(dc.obj_name, dc.index_offset)

            display_name = str(getattr(dc, 'obj_name', '') or '')
            section.append(f"; [mesh:{display_name}] [vertex_count:{dc.vertex_count}]")

            for info in obj_infos:
                condition_str = dc.get_condition_str()
                indent = "  " if condition_str else ""
                if condition_str:
                    section.append(f"if {condition_str}")
                run_lines = M_IniHelper.get_shader_replace_run_logic(
                    info,
                    dc.match_draw_ib or "0",
                    dc.match_first_index if dc.match_first_index else "0",
                    info.get('component_index', 0),
                    dc.index_count,
                    draw_offset,
                )
                for line in run_lines:
                    section.append(f"{indent}{line}")
                if condition_str:
                    section.append("endif")
            section.append("")

    def _get_all_cross_ib_identifiers(self):
        all_identifiers = set()

        if self.cross_ib_match_mode == 'INDEX_COUNT':
            for source_key, target_key_list in self.cross_ib_info_dict.items():
                if source_key.startswith('indexcount_'):
                    index_count = source_key.replace('indexcount_', '')
                    all_identifiers.add(index_count)
                for target_key in target_key_list:
                    if target_key.startswith('indexcount_'):
                        index_count = target_key.replace('indexcount_', '')
                        all_identifiers.add(index_count)

            for submesh_model in self.submesh_model_list:
                if submesh_model.match_index_count:
                    all_identifiers.add(submesh_model.match_index_count)
        else:
            for source_ib, target_ib_list in self.cross_ib_info_dict.items():
                source_hash = source_ib.split("_")[0]
                all_identifiers.add(source_hash)
                for target_ib in target_ib_list:
                    target_hash = target_ib.split("_")[0]
                    all_identifiers.add(target_hash)

            for drawib_model in self.drawib_model_list:
                all_identifiers.add(drawib_model.draw_ib)

        return all_identifiers

    def _get_vb_condition_for_mapping(self, source_ib_key, target_ib_key, condition_type='source'):
        mapping_key = (source_ib_key, target_ib_key)
        condition_info = self.cross_ib_vb_condition_mapping.get(mapping_key, {})
        if condition_type == 'source':
            return condition_info.get('source', "if vs == 200 || vs == 201 || vs == 204")
        else:
            return condition_info.get('target', "if vs == 202 || vs == 203")

    def _get_vb_condition_for_object(self, obj_name, source_ib_key, target_ib_key, condition_type='source'):
        object_mapping_key = (obj_name, source_ib_key, target_ib_key)
        condition_info = self.cross_ib_object_vb_condition.get(object_mapping_key, {})
        if condition_type == 'source':
            return condition_info.get('source', "if vs == 200 || vs == 201 || vs == 204")
        else:
            return condition_info.get('target', "if vs == 202 || vs == 203")

    def _split_drawcalls_by_cross_ib(self, drawcall_model_list, source_ib_key=None, target_ib_key=None):
        cross_ib_drawcalls = []
        non_cross_ib_drawcalls = []

        cross_ib_mapping_objects = self.cross_ib_mapping_objects

        for drawcall_model in drawcall_model_list:
            obj_name = drawcall_model.obj_name if hasattr(drawcall_model, 'obj_name') else str(drawcall_model)

            is_cross_ib = False
            if source_ib_key:
                if target_ib_key:
                    mapping_key = (source_ib_key, target_ib_key)
                    if mapping_key in cross_ib_mapping_objects:
                        if obj_name in cross_ib_mapping_objects[mapping_key]:
                            is_cross_ib = True
                else:
                    for (src_key, tgt_key), obj_names in cross_ib_mapping_objects.items():
                        if src_key == source_ib_key and obj_name in obj_names:
                            is_cross_ib = True
                            break
            else:
                if obj_name in self.cross_ib_object_names:
                    is_cross_ib = True

            if is_cross_ib:
                cross_ib_drawcalls.append(drawcall_model)
            else:
                non_cross_ib_drawcalls.append(drawcall_model)

        return cross_ib_drawcalls, non_cross_ib_drawcalls

    def _group_drawcalls_by_cross_ib_target(self, drawcall_model_list, source_ib_key, target_ib_keys):
        grouped = {}
        cross_ib_mapping_objects = self.cross_ib_mapping_objects

        for drawcall_model in drawcall_model_list:
            obj_name = drawcall_model.obj_name if hasattr(drawcall_model, 'obj_name') else str(drawcall_model)

            for target_ib_key in target_ib_keys:
                mapping_key = (source_ib_key, target_ib_key)
                if mapping_key in cross_ib_mapping_objects:
                    if obj_name in cross_ib_mapping_objects[mapping_key]:
                        vb_condition = self._get_vb_condition_for_object(obj_name, source_ib_key, target_ib_key, 'source')
                        group_key = (target_ib_key, vb_condition)
                        if group_key not in grouped:
                            grouped[group_key] = []
                        grouped[group_key].append(drawcall_model)
                        break

        return grouped

    @staticmethod
    def _get_source_cross_ib_variants(vb_condition):
        """Split the EFMI capture VS from replay VS stages."""
        condition = str(vb_condition or "").strip()
        if not condition:
            return []

        condition_match = re.fullmatch(r"if\s+(.+)", condition, re.IGNORECASE)
        if not condition_match:
            return [(condition, "CustomShader_ExtractCB1", 2)]

        filters = []
        for term in condition_match.group(1).split("||"):
            term_match = re.fullmatch(
                r"\s*\(?\s*vs\s*==\s*(\d+)\s*\)?\s*",
                term,
                re.IGNORECASE,
            )
            if not term_match:
                return [(condition, "CustomShader_ExtractCB1", 2)]
            filter_index = int(term_match.group(1))
            if filter_index not in filters:
                filters.append(filter_index)

        if 200 not in filters:
            return [(condition, "CustomShader_ExtractCB1", 2)]

        variants = [("if vs == 200", "CustomShader_ExtractCaptureCB1", 1)]
        replay_filters = [filter_index for filter_index in filters if filter_index != 200]
        if replay_filters:
            replay_condition = "if " + " || ".join(
                f"vs == {filter_index}" for filter_index in replay_filters
            )
            variants.append((replay_condition, "CustomShader_ExtractCB1", 2))
        return variants


    def _append_source_cross_ib_replay(self, section, vb_condition, objects, source_identifier):
        for condition, extract_shader, cb_slot in self._get_source_cross_ib_variants(vb_condition):
            indent = "    " if condition else ""
            if condition:
                section.append(condition)
            section.append(f"{indent}run = {extract_shader}")
            section.append(f"{indent}cs-t2 = ResourceID_{source_identifier}")
            section.append(f"{indent}run = CustomShader_RecordBones_{source_identifier}")
            section.append(f"{indent}run = CustomShader_RedirectCB1_{source_identifier}")
            section.append(f"{indent}vs-t0 = ResourceFakeT0_SRV_{source_identifier}")
            section.append(f"{indent}vs-cb{cb_slot} = ResourceFakeCB1_{source_identifier}")
            section.append(";所有需要跨 Ib 的物体引用")
            self._append_drawindexed_instanced_with_shader_replace(section, objects, None)
            if condition:
                section.append("endif")

    def _generate_cross_ib_block_for_source(self, source_identifier, drawcall_model_list, source_ib_key=None, target_ib_key=None):
        lines = []

        cross_ib_drawcalls, non_cross_ib_drawcalls = self._split_drawcalls_by_cross_ib(
            drawcall_model_list,
            source_ib_key=source_ib_key
        )

        target_ib_keys = list(self.cross_ib_source_to_target_dict.get(source_ib_key, []) or [])
        if target_ib_key and target_ib_key not in target_ib_keys:
            target_ib_keys.append(target_ib_key)

        grouped_drawcalls = self._group_drawcalls_by_cross_ib_target(cross_ib_drawcalls, source_ib_key, target_ib_keys)

        class _ListSectionAdapter:
            def __init__(self, target_lines):
                self._target_lines = target_lines

            def append(self, line):
                self._target_lines.append(line)

        section_adapter = _ListSectionAdapter(lines)

        for (tgt_ib_key, vb_condition), objects in grouped_drawcalls.items():
            if not objects:
                continue

            lines.append(";跨 iB 区域")
            self._append_source_cross_ib_replay(
                section_adapter,
                vb_condition,
                objects,
                source_identifier,
            )

        lines.append(";不需要跨 Ib 的物体引用")

        if non_cross_ib_drawcalls:
            self._append_drawindexed_instanced_with_shader_replace(
                section_adapter,
                non_cross_ib_drawcalls,
                None,
            )

        lines.append("")
        lines.append("post vs-cb1 = null")
        lines.append("post vs-cb2 = null")
        lines.append("post vs-t0 = null")
        lines.append("post cs-t2 = null")

        return lines

    def _append_cross_ib_fake_resources(self, present_section, all_identifiers):
        identifier_count = len(all_identifiers)
        max_base_offset = max(0, identifier_count - 1) * 1000
        fake_t0_array_size = max(200000, max_base_offset + 100000 + 768)

        present_section.append("[ResourceDumpedCB1_UAV]")
        present_section.append("type = RWStructuredBuffer")
        present_section.append("stride = 16")
        present_section.append("array = 4096")
        present_section.new_line()

        present_section.append("[ResourceDumpedCB1_SRV]")
        present_section.append("type = Buffer")
        present_section.append("stride = 16")
        present_section.append("array = 4096")
        present_section.new_line()

        for identifier in sorted(all_identifiers):
            present_section.append(f"[ResourceFakeCB1_UAV_{identifier}]")
            present_section.append("type = RWStructuredBuffer")
            present_section.append("stride = 16")
            present_section.append("array = 4096")
            present_section.new_line()

            present_section.append(f"[ResourceFakeCB1_{identifier}]")
            present_section.append("type = Buffer")
            present_section.append("stride = 16")
            present_section.append("format = R32G32B32A32_UINT")
            present_section.append("array = 4096")
            present_section.new_line()

            present_section.append(f"[ResourceFakeT0_UAV_{identifier}]")
            present_section.append("type = RWStructuredBuffer")
            present_section.append("stride = 16")
            present_section.append(f"array = {fake_t0_array_size}")
            present_section.new_line()

            present_section.append(f"[ResourceFakeT0_SRV_{identifier}]")
            present_section.append("type = StructuredBuffer")
            present_section.append("stride = 16")
            present_section.append(f"array = {fake_t0_array_size}")
            present_section.new_line()

        present_section.append("[ResourceFakeT0_UAV]")
        present_section.append("type = RWStructuredBuffer")
        present_section.append("stride = 16")
        present_section.append(f"array = {fake_t0_array_size}")
        present_section.new_line()

        present_section.append("[ResourceFakeT0_SRV]")
        present_section.append("type = StructuredBuffer")
        present_section.append("stride = 16")
        present_section.append(f"array = {fake_t0_array_size}")
        present_section.new_line()

        present_section.append("[ResourcePrev_SRV]")
        present_section.append("type = StructuredBuffer")
        present_section.append("stride = 16")
        present_section.append(f"array = {fake_t0_array_size}")
        present_section.new_line()

    def _add_cross_ib_present_section(self, ini_builder):
        if not self.has_cross_ib:
            return

        present_section = M_IniSection(M_SectionType.CrossIBPresent)
        present_section.append(";特殊追加固定区域")

        all_identifiers = self._get_all_cross_ib_identifiers()
        self._append_cross_ib_fake_resources(present_section, all_identifiers)

        present_section.append("[CustomShader_ExtractCB1]")
        present_section.append("vs = ./res/extract_cb1_vs.hlsl")
        present_section.append("ps = ./res/extract_cb1_ps.hlsl")
        present_section.append("ps-u7 = ResourceDumpedCB1_UAV")
        present_section.append("depth_enable = false")
        present_section.append("blend = ADD SRC_ALPHA INV_SRC_ALPHA")
        present_section.append("cull = none")
        present_section.append("topology = point_list")
        present_section.append("draw = 4096, 0")
        present_section.append("ps-u7 = null")
        present_section.append("ResourceDumpedCB1_SRV = copy ResourceDumpedCB1_UAV")
        present_section.new_line()

        present_section.append("[CustomShader_ExtractCaptureCB1]")
        present_section.append("vs = ./res/extract_capture_cb1_vs.hlsl")
        present_section.append("ps = ./res/extract_cb1_ps.hlsl")
        present_section.append("ps-u7 = ResourceDumpedCB1_UAV")
        present_section.append("depth_enable = false")
        present_section.append("blend = ADD SRC_ALPHA INV_SRC_ALPHA")
        present_section.append("cull = none")
        present_section.append("topology = point_list")
        present_section.append("draw = 4096, 0")
        present_section.append("ps-u7 = null")
        present_section.append("ResourceDumpedCB1_SRV = copy ResourceDumpedCB1_UAV")
        present_section.new_line()

        for identifier in sorted(all_identifiers):
            present_section.append(f"[CustomShader_RecordBones_{identifier}]")
            present_section.append("cs = ./res/record_bones_cs.hlsl")
            present_section.append("cs-t0 = vs-t0")
            present_section.append("cs-t1 = ResourceDumpedCB1_SRV")
            present_section.append(f"cs-u1 = ResourceFakeT0_UAV_{identifier}")
            present_section.append("dispatch = 12, 1, 1")
            present_section.append("cs-u1 = null")
            present_section.append("cs-t0 = null")
            present_section.append("cs-t1 = null")
            present_section.append(f"ResourceFakeT0_SRV_{identifier} = copy ResourceFakeT0_UAV_{identifier}")
            present_section.new_line()

            present_section.append(f"[CustomShader_RedirectCB1_{identifier}]")
            present_section.append("cs = ./res/redirect_cb1_cs.hlsl")
            present_section.append("cs-t0 = ResourceDumpedCB1_SRV")
            present_section.append(f"ResourceFakeCB1_UAV_{identifier} = copy ResourceDumpedCB1_SRV")
            present_section.append(f"cs-u0 = ResourceFakeCB1_UAV_{identifier}")
            present_section.append("dispatch = 4, 1, 1")
            present_section.append("cs-u0 = null")
            present_section.append("cs-t0 = null")
            present_section.append(f"ResourceFakeCB1_{identifier} = copy ResourceFakeCB1_UAV_{identifier}")
            present_section.new_line()

        shader_overrides = [
            ("ShaderOverridevs1000", "f11c7e1dbf876a69", "200"),
            ("ShaderOverridevs1001", "303f45d5266d0369", "201"),
            ("ShaderOverridevs1002", "7b3a141f99cd9b39", "201"),
            ("ShaderOverridevs1003", "1479b2b594b9c91a", "202"),
            ("ShaderOverridevs1004", "c6e55aaa8f4b3218", "202"),
            ("ShaderOverridevs1005", "784f11ae11c97112", "203"),
            ("ShaderOverridevs1006", "f1b10202c73c72c3", "204"),
            ("ShaderOverridevs1007", "12ad3cc5f56f853c", "204"),
            ("ShaderOverridevs1008", "86cb3bc0a3e2e013", "204"),
            ("ShaderOverridevs1009", "906a3976f3e33cfb", "204"),
            ("ShaderOverridevs1010", "0ba16985f9f74f8d", "204"),
            ("ShaderOverridevs1011", "06c94dd56f447210", "204"),
            ("ShaderOverridevs1012", "f47b1f797f5831d0", "204"),
        ]

        for name, hash_val, filter_idx in shader_overrides:
            present_section.append(f"[{name}]")
            present_section.append(f"hash = {hash_val}")
            present_section.append(f"filter_index = {filter_idx}")
            present_section.append("allow_duplicate_hash = overrule")
            present_section.new_line()

        ini_builder.append_section(present_section)


    def _add_cross_ib_resource_id_sections(self, ini_builder):
        if not self.has_cross_ib:
            return

        resource_id_section = M_IniSection(M_SectionType.ResourceID)
        resource_id_section.append(";特殊追加身份证区域")

        all_identifiers = set()

        if self.cross_ib_match_mode == 'INDEX_COUNT':
            for source_key, target_key_list in self.cross_ib_info_dict.items():
                if source_key.startswith('indexcount_'):
                    index_count = source_key.replace('indexcount_', '')
                    all_identifiers.add(index_count)
                for target_key in target_key_list:
                    if target_key.startswith('indexcount_'):
                        index_count = target_key.replace('indexcount_', '')
                        all_identifiers.add(index_count)

            for submesh_model in self.submesh_model_list:
                if submesh_model.match_index_count:
                    all_identifiers.add(submesh_model.match_index_count)
        else:
            for source_ib, target_ib_list in self.cross_ib_info_dict.items():
                source_hash = source_ib.split("_")[0]
                all_identifiers.add(source_hash)
                for target_ib in target_ib_list:
                    target_hash = target_ib.split("_")[0]
                    all_identifiers.add(target_hash)

            for drawib_model in self.drawib_model_list:
                all_identifiers.add(drawib_model.draw_ib)

        sorted_identifiers = sorted(list(all_identifiers))

        for idx, identifier in enumerate(sorted_identifiers):
            resource_id_section.append(f"[ResourceID_{identifier}]")
            resource_id_section.append("type = Buffer")
            resource_id_section.append("format = R32_FLOAT")
            resource_id_section.append(f"data = {idx * 1000}.0")
            resource_id_section.new_line()

        ini_builder.append_section(resource_id_section)

    def _find_source_submesh_by_ib_key(self, source_ib_key):
        for submesh_model in self.submesh_model_list:
            submesh_ib_key = self._get_submesh_ib_key(submesh_model)
            if submesh_ib_key == source_ib_key:
                return submesh_model
        return None

    def _find_source_drawib_by_ib_key(self, source_ib_key):
        if self.cross_ib_match_mode == 'INDEX_COUNT':
            index_count = source_ib_key.replace('indexcount_', '') if source_ib_key.startswith('indexcount_') else None
            if index_count:
                for drawib_model in self.drawib_model_list:
                    for submesh in drawib_model.submesh_model_list:
                        if submesh.match_index_count == index_count:
                            return drawib_model
            return None
        else:
            source_hash = source_ib_key.split("_")[0]
            for drawib_model in self.drawib_model_list:
                if drawib_model.draw_ib == source_hash:
                    return drawib_model
            return None

    @staticmethod
    def _lod_name_from_unique_str(unique_str: str) -> str:
        """解析 unique_str 的 LOD 前缀（'LOD0.xxx' -> 'LOD0'；无前缀 -> ''）。

        与 WorkSpaceHelper.parse_lod_unique_str 语义一致；用于排序/统计合并骨架
        组件（同一骨架缓冲内的组件按其 LOD 段排序分配全局 id）。
        """
        normalized = str(unique_str or "").strip()
        if normalized.upper().startswith("LOD") and "." in normalized:
            dot_idx = normalized.index(".")
            potential = normalized[:dot_idx]
            if potential[3:].isdigit():
                return potential
        return ""

    @staticmethod
    def _validated_blendindices_layouts(submesh_models, context: str):
        """返回实际 BLENDINDICES 布局；同一运行时命令列表内必须完全一致。

        EFMI 骨骼合并全池共用一套粘合层，运行时对 vb 槽位只声明一种
        BLENDINDICES ElementFormat（见 _add_merged_skeleton_section），
        因此所有合并部件的最终布局必须一致（跨 LOD 亦然）。格式宽度
        （R8/R16/R32）在 widen_blendindices 阶段已统一归一化到 R16 系
        （d3d11_gametype.py）；此处仍不一致属于结构性数据问题（通道数 /
        SemanticIndex / ExtractSlot 不同），需要修正差异部件的来源数据后重新导入。
        """
        submesh_models = list(submesh_models)
        if not submesh_models:
            raise RuntimeError(f"{context}: 未找到对应子网格，无法确定 BLENDINDICES 布局")
        expected = None
        expected_unique_str = None
        for submesh_model in submesh_models:
            game_type = getattr(submesh_model, "d3d11_game_type", None)
            if game_type is None:
                raise RuntimeError(f"{context}: 子网格缺少 GameType")
            layouts = tuple(game_type.get_blendindices_layouts())
            if not layouts:
                raise RuntimeError(
                    f"{context}: {getattr(submesh_model, 'unique_str', '?')} "
                    "不含 BLENDINDICES 布局"
                )
            if expected is None:
                expected = layouts
                expected_unique_str = getattr(submesh_model, "unique_str", "?")
            elif layouts != expected:
                raise RuntimeError(
                    f"{context}: 全池的 BLENDINDICES 布局不一致\n"
                    f"  基准部件 {expected_unique_str}: {expected}\n"
                    f"  差异部件 {getattr(submesh_model, 'unique_str', '?')}: {layouts}\n"
                    "格式宽度（R8/R16/R32）已自动统一为 R16 系；若仍不一致，请检查"
                    "差异部件的通道数 / SemanticIndex / ExtractSlot 与同 LOD 其他部件"
                    "是否相同，修正来源数据后重新导入该部件"
                )
        return expected or ()

    @staticmethod
    def _build_same_ib_bone_aliases(baseline_model, lod_model) -> dict[int, int]:
        """把被折叠 LOD 部件自有槽位映射到 same-IB 基准部件槽位。

        same-IB 只有一个游戏 draw，因此只能有一个 EntryPoint/component。非基准
        LOD 的其它部件仍可能引用被折叠部件的独立槽位；若不重定向，这些槽位在
        合并导出中无人写入，网格会读取未初始化矩阵并爆炸。

        只重定向落在 ``lod_model`` 自有声明区间内的 VGMap 值。映射到其它仍存在
        component 的去重值必须保留，不能误把整个 VGMap 都投影回基准 LOD。
        """
        lod_vg_map = dict(getattr(lod_model, "vg_map", {}) or {})
        if not lod_vg_map:
            return {}

        lod_start = int(getattr(lod_model, "vg_offset", 0) or 0)
        lod_end = lod_start + int(getattr(lod_model, "vg_count", 0) or 0)
        baseline_start = int(getattr(baseline_model, "vg_offset", 0) or 0)
        baseline_count = int(getattr(baseline_model, "vg_count", 0) or 0)
        correspondence = dict(
            getattr(lod_model, "efmi_lod_correspondence", {}) or {}
        )
        baseline_unique = str(getattr(baseline_model, "unique_str", "") or "")

        aliases: dict[int, int] = {}
        for raw_local_id, raw_global_id in lod_vg_map.items():
            local_id = int(raw_local_id)
            source_global_id = int(raw_global_id)
            if not (lod_start <= source_global_id < lod_end):
                continue

            corr = correspondence.get(str(local_id), correspondence.get(local_id))
            if not isinstance(corr, dict) or "local_vg_id" not in corr:
                raise RuntimeError(
                    f"[EFMI骨骼合并] {getattr(lod_model, 'unique_str', '?')} "
                    f"的自有骨骼 local {local_id}（全局槽 {source_global_id}）缺少跨 LOD 对应；"
                    "same-IB 部件无法安全折叠。请重新执行骨骼合并反查/重新导入该角色"
                )

            # I3 折叠蒙皮兼容性：same-IB 折叠要求 L1 骨骼与基准骨骼语义一致。
            # matrix_diff 超过阈值说明该 local 被对应到基准侧另一根骨骼
            # （实测错误对应 272~449），静默折叠会把 L1 蒙皮语义套到 L0 骨骼段，
            # 必须大声拒绝而不是产出错误权重。缺 matrix_diff 的旧账本按兼容
            # 处理（不阻塞既有元数据），有值就必须通过阈值校验。
            # 用户领域裁决（2026-09-01，已拍板）：折叠判据 = **rotation-only**
            # ``matrix_diff_rotation``（旋转/尺度块差，去掉捕获帧世界平移伪影——
            # 跨环境捕获固有，世界平移 = 捕获帧角色世界位置的环境噪声，不携带
            # 骨骼身份；佩丽卡脸部 raw 368-409 而旋转语义 53/53 <2.0）；旧账本无
            # 该字段时回退 raw matrix_diff（行为不变）。阈值 16.0/1.0 未放宽：
            # 旋转/尺度真实不兼容时 rotation 差仍超阈值 → 拒绝；用户明确不再补
            # n<3 守卫 / 中位数残差 / 审计日志（接受现状）。
            matrix_diff = corr.get("matrix_diff_rotation", corr.get("matrix_diff"))
            if matrix_diff is not None:
                try:
                    parsed_matrix_diff = float(matrix_diff)
                except (TypeError, ValueError):
                    parsed_matrix_diff = float("inf")
                component_score = corr.get("component_score")
                centroid_distance = corr.get("centroid_distance")
                try:
                    parsed_component_score = float(component_score)
                    parsed_centroid_distance = float(centroid_distance)
                except (TypeError, ValueError):
                    parsed_component_score = float("inf")
                    parsed_centroid_distance = float("inf")
                baseline_local_id = int(corr.get("local_vg_id", local_id))
                strong_geometry_evidence = (
                    baseline_local_id == local_id
                    and numpy.isfinite(parsed_component_score)
                    and parsed_component_score <= _SAME_IB_FOLD_MAX_COMPONENT_SCORE
                    and numpy.isfinite(parsed_centroid_distance)
                    and parsed_centroid_distance <= _SAME_IB_FOLD_MAX_CENTROID_DISTANCE
                )
                allowed_matrix_diff = (
                    _SAME_IB_FOLD_STRONG_GEOMETRY_MAX_MATRIX_DIFF
                    if strong_geometry_evidence
                    else _SAME_IB_FOLD_MAX_MATRIX_DIFF
                )
                if (
                    not numpy.isfinite(parsed_matrix_diff)
                    or parsed_matrix_diff > allowed_matrix_diff
                ):
                    raise RuntimeError(
                        f"[EFMI骨骼合并] {getattr(lod_model, 'unique_str', '?')} "
                        f"local {local_id}（全局槽 {source_global_id}）的跨 LOD 对应骨骼"
                        f" matrix_diff={parsed_matrix_diff:.3f} 超过 same-IB 折叠阈值 "
                        f"{allowed_matrix_diff}（强几何证据={strong_geometry_evidence}）："
                        "L1 蒙皮语义与基准部件不一致，"
                        "拒绝静默折叠。请重新执行骨骼合并反查/重新导入该角色"
                    )
            reference_component = str(
                corr.get("unique_str", corr.get("reference_component", ""))
                or getattr(lod_model, "efmi_lod_reference_component", "")
                or ""
            )
            if reference_component != baseline_unique:
                raise RuntimeError(
                    f"[EFMI骨骼合并] {getattr(lod_model, 'unique_str', '?')} "
                    f"local {local_id} 的跨 LOD 参考部件为 "
                    f"{reference_component or '空'}，但 same-IB 基准是 {baseline_unique}；"
                    "元数据不一致，请重新执行骨骼合并反查/重新导入该角色"
                )

            baseline_local_id = int(corr.get("local_vg_id", local_id))
            if not (0 <= baseline_local_id < baseline_count):
                raise RuntimeError(
                    f"[EFMI骨骼合并] {getattr(lod_model, 'unique_str', '?')} "
                    f"local {local_id} 对应到越界的基准 local {baseline_local_id} "
                    f"（{baseline_unique} 仅有 {baseline_count} 组）；请重新执行骨骼合并反查"
                )
            # MergedSkeleton_AttachComponent 并不读取 JSON 的 VGMap。它始终把
            # 当前 draw 的 original_bone_id 写入 ``vg_offset + local_id``。
            # 因而 same-IB 折叠后，LOD 槽位必须指向基准 component 的连续导入槽，
            # 不能指向 baseline_vg_map 中的跨 component 去重槽。后一种槽位可能
            # 属于只在另一 LOD 出现的 component，当前帧无人写入，表现为权重存在
            # 但绑定到错误矩阵、运动时整片扭曲。
            target_global_id = baseline_start + baseline_local_id
            if not (0 <= source_global_id <= 65535 and 0 <= target_global_id <= 65535):
                raise RuntimeError(
                    "[EFMI骨骼合并] same-IB 骨骼别名超出 R16 范围: "
                    f"{source_global_id} -> {target_global_id}"
                )
            aliases[source_global_id] = target_global_id
        return aliases

    def _rekey_same_ib_aliases_by_export_identity(
        self, aliases: dict[int, int]
    ) -> dict[int, int]:
        """t10（方案 A4）：把 same-IB 折叠 aliases 的 source 键（槽位号）换算为
        双套导出身份 e(s)。

        合并模式应用双套更名后，写盘缓冲的 BLENDINDICES = e(s)（合并槽最强源组
        身份，非槽位号）。折叠 aliases 的 source（被折叠 LOD 部件自有声明段的
        槽位号）若更名，原槽位号在缓冲中已不存在，remap 永不命中 → 该 LOD 的
        折叠骨骼引用无人重定向 → 读未初始化槽 → 爆炸。本方法把更名过的 source
        键换为 e(s)（单源槽 e(s)==s 原样保留）。

        边界：
        - 无工作区 / 建表失败（A3/A4/B10 或未初始化）：不换算、保持原 aliases
          （折叠既有行为不变），并打警告——调用方若同时生效了更名会导致折叠
          失配，由更名侧 fail-closed 兜底；
        - e(s) 注入性（规格 §1.3 推论 1/2）：更名目标不在槽位集合内 ⇒ 不会与
          其它未更名 source 键撞车；防御性复查仍保留（损坏数据时大声失败）。
        """
        if not aliases:
            return aliases
        _workspace_folder = getattr(GlobalConfig, "path_workspace_folder", None)
        try:
            workspace_root = (
                str(_workspace_folder() or "").strip()
                if callable(_workspace_folder)
                else ""
            )
        except Exception:
            workspace_root = ""
        if not workspace_root:
            print(
                "[EFMI骨骼合并] 警告：无法定位工作区，same-IB 折叠 aliases "
                "未按双套导出身份换算（保持槽位键语义）"
            )
            return aliases
        from ...common.efmi_skeleton import EFMIBoneMapBuilder

        try:
            table = EFMIBoneMapBuilder.get_dualset_export_table_cached(
                workspace_root
            )
        except RuntimeError as exc:
            print(
                "[EFMI骨骼合并] 警告：折叠 aliases 身份换算跳过 "
                f"（建表失败: {str(exc)[:120]}），保持槽位键语义"
            )
            return aliases
        if not table:
            return aliases
        e_of = {
            slot: row["export_identity"]
            for slot, row in table.items()
            if row.get("renamed")
        }
        if not e_of:
            return aliases
        rekeyed: dict[int, int] = {}
        changed = False
        for source_id, target_id in aliases.items():
            new_source = e_of.get(source_id, source_id)
            if new_source in rekeyed and rekeyed[new_source] != target_id:
                raise RuntimeError(
                    "[EFMI骨骼合并] same-IB 折叠 aliases 按导出身份换算后 "
                    f"source {new_source} 映射冲突（{rekeyed[new_source]} != "
                    f"{target_id}）——导出身份非注入，数据损坏或陈旧，拒绝导出"
                )
            if new_source != source_id:
                changed = True
            rekeyed[new_source] = target_id
        return rekeyed if changed else aliases

    @staticmethod
    def _validate_merged_slot_reachability(
        parts: list[dict],
        component_id_dict: dict[str, int],
        submesh_models,
        bone_aliases: dict[int, int],
        export_identity_map: dict[int, int] | None = None,
        per_mesh_maps: dict[str, dict[int, int]] | None = None,
    ) -> None:
        """I2 导出前可达性守卫：每个存活 Blend 引用的槽在其出现距离必被维护者写入。

        存活部件 P（含 same-IB 折叠家族）绘制于距离集 D(P)：基准 L0 部件 =
        {LOD0} ∪ 被折叠同 IB 兄弟所在 LOD（同一 draw 单入口，任何距离都执行
        同一 EntryPoint，写 P 自己的声明段）；独立 LOD1 部件 = {LOD1}。

        每个写盘索引 t 属于 P 家族引用的 Blend 索引集合（S5：t10 双套更名后
        写盘缓冲为导出身份 e(s) 而非槽位——先用 export_identity_map 把 VGMap
        槽位换算为 e(s)，再经 same-IB 别名（值亦为身份）重定向）：
        - t 落在 P 自己的声明段 → 由 P 自己写入 → 安全；
        - 否则 t 落在另一部件 Q 的声明段 → 由 Q 的绘制写入 → 必须满足
          D(P) ⊆ D(Q)（Q 在 P 出现的每个距离都绘制），不满足 → RuntimeError
          指名部件/索引号，与既有槽位重叠/R16 越界守卫同风格（fail-closed）；
        - t 落在任何导出部件声明段之外（局部导出子集未包含维护者）→ 无法归属，
          仅告警不阻塞导出（与既有快速/局部导出语义一致）。

        单 LOD 导出（少于 2 个带前缀 LOD）不存在跨距离悬空问题，直接放行。
        """
        part_by_id = {int(p["component_id"]): p for p in parts}
        if len(part_by_id) <= 1:
            return
        # 单 LOD 判定用整个池的家族距离集（含被折叠的 same-IB 兄弟），
        # 不能只看 parts 的 unique_str——基准部件的 LOD 名只有基准那一侧。
        pool_lod_names = {
            ExportEFMI._lod_name_from_unique_str(str(unique_str))
            for unique_str in component_id_dict
        }
        if len(pool_lod_names - {""}) < 2:
            return
        segments = [
            (
                p,
                int(p["vg_offset"]),
                int(p["vg_offset"]) + int(p["vg_count"]),
            )
            for p in parts
        ]
        draw_distances: dict[int, frozenset] = {}
        for unique_str, component_id in component_id_dict.items():
            current = set(draw_distances.get(int(component_id), ()))
            current.add(ExportEFMI._lod_name_from_unique_str(str(unique_str)))
            draw_distances[int(component_id)] = frozenset(current - {""})
        aliases = {int(k): int(v) for k, v in (bone_aliases or {}).items()}
        e_of = {int(k): int(v) for k, v in (export_identity_map or {}).items()}
        for part in parts:
            part_component_id = int(part["component_id"])
            part_lods = draw_distances.get(part_component_id, frozenset())
            for model in submesh_models:
                model_unique = str(getattr(model, "unique_str", "") or "")
                if component_id_dict.get(model_unique) != part_component_id:
                    continue
                vg_map = dict(getattr(model, "vg_map", {}) or {})
                # t25/per-mesh（v3）：写盘缓冲索引 = 该模型自己的 per-mesh 身份
                # （本组件成员身份，含未更名槽/canonical 跨组件槽），非全局
                # e(s)——先按 model 的 per-mesh map 换算，缺省回退全局 e_of，
                # 再经折叠别名（值为身份）重定向。
                pm_map = (per_mesh_maps or {}).get(model_unique, {}) or {}
                ref_slots = set()
                for raw_slot in vg_map.values():
                    value = int(pm_map.get(int(raw_slot), int(e_of.get(int(raw_slot), int(raw_slot)))))
                    ref_slots.add(int(aliases.get(value, value)))
                for slot in sorted(ref_slots):
                    owner = next(
                        (
                            q
                            for q, q_start, q_end in segments
                            if q_start <= slot < q_end
                        ),
                        None,
                    )
                    if owner is None:
                        print(
                            f"[EFMI骨骼合并] 警告 {model_unique} 引用槽 {slot} "
                            "不属于任何导出部件声明段（局部导出未包含维护者，"
                            "跳过可达性校验）"
                        )
                        continue
                    if int(owner["component_id"]) == part_component_id:
                        continue
                    owner_lods = draw_distances.get(
                        int(owner["component_id"]), frozenset()
                    )
                    if not part_lods.issubset(owner_lods):
                        raise RuntimeError(
                            f"[EFMI骨骼合并] {model_unique} 引用槽 {slot} "
                            f"由部件 {owner['unique_str']} 的声明段维护，但该部件只在 "
                            f"{sorted(owner_lods) or '无前缀'} 距离绘制，"
                            f"而引用网格在 {sorted(part_lods)} 距离出现："
                            "该槽在部分 LOD 距离无人写入（跨组件悬空骨骼引用，"
                            "L1 距离蒙皮会冻结在 identity）。请重新执行骨骼合并反查/"
                            "重新导入该角色后导出"
                        )

    @staticmethod
    def _remap_blendindices_category_buffer(
        category_buf,
        category_name: str,
        d3d11_game_type,
        aliases: dict[int, int],
    ):
        """在写盘副本中重定向 R16 BLENDINDICES；不修改 SubMeshModel 原缓冲。"""
        raw = numpy.ascontiguousarray(category_buf)
        if raw.dtype != numpy.uint8:
            raw = raw.view(numpy.uint8)
        raw = raw.reshape(-1)
        stride = int(
            getattr(d3d11_game_type, "CategoryStrideDict", {}).get(
                category_name, 0
            )
            or 0
        )
        if not aliases or stride <= 0 or raw.size == 0:
            return raw, 0
        if raw.size % stride != 0:
            raise RuntimeError(
                f"[EFMI骨骼合并] {category_name} 缓冲大小 {raw.size} "
                f"不能被 stride {stride} 整除"
            )

        rows = raw.copy().reshape(-1, stride)
        category_offset = 0
        remapped_count = 0
        for element in getattr(d3d11_game_type, "D3D11ElementList", []):
            if str(getattr(element, "Category", "") or "") != category_name:
                continue
            width = int(getattr(element, "ByteWidth", 0) or 0)
            semantic = str(getattr(element, "SemanticName", "") or "").upper()
            if semantic == "BLENDINDICES":
                element_format = str(getattr(element, "Format", "") or "").upper()
                if not (
                    element_format.startswith("R16")
                    and element_format.endswith(("_UINT", "_SINT"))
                    and width % 2 == 0
                ):
                    raise RuntimeError(
                        "[EFMI骨骼合并] same-IB 骨骼重定向要求 R16 整数 "
                        f"BLENDINDICES，实际为 {element_format}"
                    )
                field_bytes = rows[
                    :, category_offset:category_offset + width
                ].copy()
                values = field_bytes.view("<u2").reshape(len(rows), width // 2)
                for source_id, target_id in aliases.items():
                    mask = values == int(source_id)
                    hit_count = int(numpy.count_nonzero(mask))
                    if hit_count:
                        values[mask] = int(target_id)
                        remapped_count += hit_count
                rows[:, category_offset:category_offset + width] = (
                    values.view(numpy.uint8).reshape(len(rows), width)
                )
            category_offset += width

        return rows.reshape(-1), remapped_count

    @staticmethod
    def _missing_fold_baseline_unique_str(
        workspace_root: str,
        model,
        present_baseline_by_unique: dict,
    ) -> str:
        """F2（t5 修复）：返回「缺基准的 same-IB 折叠候选」的基准 unique_str。

        判定：给定非基准 LOD 模型（未折叠路径），若工作空间存在**同 bare** 的
        基准部件——bare 名 = ``<drawib>-<count>-<first>``，同 bare ⟺ 同
        IB/draw 三元组 ⟺ 同一游戏 draw（same-IB 折叠家族）——且其 json 携带
        有效 VGMap（GPU 合并候选，非 CPU/投影排除），但该基准不在本次导出批次
        （``present_baseline_by_unique``）中 → 返回该基准 unique_str（调用方
        fail-closed 中止导出）；否则返回空串（真独立部件/基准已在批次内/无
        工作空间上下文，放行）。

        - 同 bare 判定不依赖跨 LOD 对应账本的 reference_component（几何配对
          部件的 reference bare ≠ 自身 bare，不会误判；无账本的旧数据仍按
          ``LOD0.<bare>`` 前缀兜底识别）；
        - 基准候选唯一按 bare 推导，不读对象 match 属性（该属性在缺基准场景
          下无从取得），与 ``_baseline_draw_key`` 的 C8 同源口径一致；
        - 缺 efmi_skeleton 模块的环境（单元测试 fake 包）按目录形态+VGMap
          轻量探测退化为同样语义（生产环境始终走模块权威实现）。
        """
        try:
            from ...common.efmi_skeleton import EFMISkeletonMergeHelper
        except ImportError:
            EFMISkeletonMergeHelper = None

        def _probe_json_path(candidate: str) -> str:
            if EFMISkeletonMergeHelper is not None:
                try:
                    return EFMISkeletonMergeHelper._resolve_submesh_json_path(
                        workspace_root, candidate
                    )
                except Exception:
                    return ""
            if not workspace_root or not os.path.isdir(workspace_root):
                return ""
            lod, bare = candidate.split(".", 1) if "." in candidate else ("", candidate)
            submesh_dir = os.path.join(
                os.path.join(workspace_root, lod) if lod else workspace_root, bare
            )
            if not os.path.isdir(submesh_dir):
                return ""
            for dirname in sorted(os.listdir(submesh_dir)):
                if not dirname.startswith("TYPE_"):
                    continue
                probe = os.path.join(submesh_dir, dirname, bare + ".json")
                if os.path.isfile(probe):
                    return probe
            return ""

        def _has_vgmap(payload) -> bool:
            if EFMISkeletonMergeHelper is not None:
                try:
                    return EFMISkeletonMergeHelper._has_valid_vgmap(payload)
                except Exception:
                    pass
            vg_map = payload.get("VGMap") if isinstance(payload, dict) else None
            if not isinstance(vg_map, dict) or not vg_map:
                return False
            try:
                vg_count = int(payload.get("VGCount", 0) or 0)
                vg_offset = int(payload.get("VGOffset", -1) or -1)
            except (TypeError, ValueError):
                return False
            return vg_count > 0 and vg_offset >= 0

        own = str(getattr(model, "unique_str", "") or "").strip()
        if "." not in own:
            return ""
        _lod_name, bare = own.split(".", 1)
        if not bare:
            return ""
        candidates: list[str] = []
        ref_component = str(
            getattr(model, "efmi_lod_reference_component", "") or ""
        ).strip()
        if "." in ref_component:
            _ref_lod, ref_bare = ref_component.split(".", 1)
            if ref_bare == bare:
                candidates.append(ref_component)
        fallback_candidate = "LOD0." + bare
        if fallback_candidate not in candidates:
            candidates.append(fallback_candidate)
        for candidate in candidates:
            # 基准已在本次导出批次内：同 bare 必同 key，未被折叠属数据不一致
            # 或键异常——同样不能静默按独立部件导出，返回候选让调用方中止。
            if candidate in present_baseline_by_unique:
                return candidate
            json_path = _probe_json_path(candidate)
            if not json_path:
                continue
            try:
                payload = JsonUtils.LoadFromFile(json_path)
            except Exception:
                continue
            if _has_vgmap(payload):
                return candidate
        return ""

    def _get_merged_skeleton_component_info(self):
        """收集 EFMI 骨骼合并（Merged Skeleton）逻辑部件（v10/v11/v13）。

        **每 LOD 槽位段相互独立**（撤销 v9 投影）：LOD0 段 0..max0、LOD1 段
        base 起（见 EFMIBoneMapBuilder.build_independent_lod_maps），两域不相交、
        全局唯一；v11/v13 追加镜像约束（L0 合并组 ⇒ L1 对应组合并、不同 L0 组
        断边），值域不做压缩重排（v12 重排实测游戏内乱掉，已撤销）。因此：

        - 每个子网格（含每个 LOD 版本）= 自己的 component，`vg_offset/vg_count`
          取**该子网格自己的** json 元数据（v10 编号写入后的分段槽位）；
        - 每个 component 只有一个绘制入口（自身换绑资源；`$lod_level` 仅保留
          在 draws 元数据里供诊断/测试，不再写入 INI——见下方裸版桥接移除说明），
          `remap` 恒 None——运行时 MergedSkeleton_Apply 把当前
          LOD draw 的**自己的**矩阵写入**自己的**槽位段，跨 LOD 零共享；
        - **same-IB 跨 LOD 部件**（脸部件等：两个 LOD 状态共用同一 IB/draw）：
          LOD1 版本不生成独立 component，component_id_dict 映射到基准部件
          的 component_id——同一 draw 只能有一个 EntryPoint（同 hash 会冲突），
          参考插件 same-IB 规则 = 单入口、不做 LOD 检测，网格数据即基准网格；
          同时把其它 LOD 网格对被折叠槽位的引用重定向到基准部件对应槽位，避免
          独立 LOD 编号留下“被引用但无人写入”的骨骼区间。

        v9 之所以爆炸：LOD1 顶点组编号投影进 LOD0 槽位 + 单池单骨架缓冲，
        运行时对同一 component 每帧只导入一次骨骼、且仅允许更优（更小）
        $lod_level 覆盖；同帧先 LOD0 后 LOD1（或混合状态）时 LOD1 网格读取的
        是 LOD0 已导入的矩阵（L0/L1 两侧矩阵数据不同）→ 顶点爆炸。分段平移后
        各 LOD 槽位域互不相交，LOD1 draw 的导入/读取永远落在自己的域内。

        返回 (parts, component_id_dict)；component_id_dict: unique_str ->
        所属部件 component_id（绘制入口据此发 EntryPoint）。
        """
        parts: list[dict] = []
        component_id_dict: dict[str, int] = {}
        same_ib_bone_aliases: dict[int, int] = {}
        same_ib_alias_targets_by_lod: dict[str, set[int]] = {}
        # FC-4（C9）：same-IB 折叠家族 LOD1 声明段（运行时死段）与别名目标记录
        self._efmi_folded_dead_segments: list[dict] = []
        self._efmi_fold_alias_targets: dict[int, int] = {}

        def _lod_name(unique_str):
            return self._lod_name_from_unique_str(unique_str)

        eligible = []
        for submesh_model in self.submesh_model_list:
            if not bool(
                getattr(submesh_model, "merged_skeleton_metadata_valid", True)
            ):
                print(
                    f"[EFMI骨骼合并] 警告 {getattr(submesh_model, 'unique_str', '?')}: "
                    "骨骼合并元数据含非整数/越界值，该部件不进入合并骨架；"
                    "请重新生成骨骼合并缓存"
                )
                continue
            vg_count = int(getattr(submesh_model, "vg_count", 0) or 0)
            if not (
                vg_count > 0
                and bool(
                    getattr(
                        getattr(submesh_model, "d3d11_game_type", None),
                        "GPU_PreSkinning",
                        False,
                    )
                )
            ):
                continue
            eligible.append(submesh_model)

        # 被预处理成全局骨骼编号的部件不能再静默退回普通绘制：普通绘制不会运行
        # MergedSkeleton_AttachComponent，全局编号会被当作原始局部编号读取，结果同样
        # 是绑定错位。单池只能声明一种布局，因此混合通道数/槽位时直接拒绝导出。
        if eligible:
            self._validated_blendindices_layouts(
                eligible,
                "[EFMI骨骼合并] 合并候选",
            )

        # 多 LOD 单池只接受当前 v13 的“各 LOD 独立 + 分段平移”编号。
        # 旧版数据即使区间表面上不重叠，也可能带投影/压缩语义；继续导出会让
        # 正确格式的数据读取错误矩阵。单 LOD 不依赖此账本，保持兼容。
        eligible_lods = {_lod_name(model.unique_str) for model in eligible}
        if len(eligible_lods) > 1:
            stale = []
            for model in eligible:
                version = int(
                    getattr(model, "efmi_lod_layout_version", 0) or 0
                )
                if version != _EFMI_CROSS_LOD_LAYOUT_VERSION:
                    stale.append(f"{model.unique_str}=v{version}")
            if stale:
                preview = ", ".join(stale[:12])
                suffix = "..." if len(stale) > 12 else ""
                raise RuntimeError(
                    "[EFMI骨骼合并] 跨 LOD 骨骼缓存版本不兼容："
                    f"当前需要 v{_EFMI_CROSS_LOD_LAYOUT_VERSION}，发现 {preview}{suffix}。"
                    "请重新执行骨骼合并反查或重新导入该角色后再合并导出"
                )

        # 基准 LOD：优先 LOD0（含根目录工作空间）。
        lod_names = {_lod_name(m.unique_str) for m in eligible}
        baseline_lod = "LOD0" if "LOD0" in lod_names else ""

        baseline_models = sorted(
            (
                m for m in eligible
                if _lod_name(m.unique_str) == baseline_lod
            ),
            key=lambda m: (
                _lod_name(m.unique_str),
                int(getattr(m, "vg_offset", 0) or 0),
            ),
        )
        non_baseline_models = sorted(
            (
                m for m in eligible
                if _lod_name(m.unique_str) != baseline_lod
            ),
            key=lambda m: (
                int(getattr(m, "vg_offset", 0) or 0),
                getattr(m, "unique_str", ""),
            ),
        )

        def _baseline_draw_key(model):
            """基准部件相同 IB/draw 判定键（same-IB 合并专用）。

            C8（P6 同源）：IB 判定键与 ``common/efmi_skeleton.efmi_ib_key``
            （I1 生命周期域同源实现）保持一致——match_draw_ib 缺失时回退按
            unique_str 推导（同一 draw 的各 LOD 版本共享同一 IB hash），防止
            三视图漂移导致折叠漏判。匹配 draw_ib 缺省时**优先 import 调用
            efmi_ib_key 单一实现**（T6-F6 消除复制粘贴漂移）；仅当模块不可达
            （测试夹具 fake 包）才退化为内联同算法，并有等价性单测兜底
            （tests/test_efmi_export_identity_separation.py::IbKeyEquivalenceTests）。
            """
            draw_ib = str(getattr(model, "match_draw_ib", "") or "")
            if not draw_ib:
                raw_unique = str(
                    getattr(model, "workspace_unique_str", "") or ""
                ) or str(getattr(model, "unique_str", "") or "")
                try:
                    from ...common.efmi_skeleton import efmi_ib_key as _efmi_ib_key_provider
                except ImportError:
                    _efmi_ib_key_provider = None
                if _efmi_ib_key_provider is not None:
                    draw_ib = _efmi_ib_key_provider(raw_unique)
                else:
                    # 测试夹具环境缺 efmi_skeleton 模块时的内联兜底——与
                    # efmi_ib_key 同算法（去 LOD 前缀 + 首段），等价性由
                    # IbKeyEquivalenceTests 全量断言。
                    if raw_unique.upper().startswith("LOD") and "." in raw_unique:
                        raw_unique = raw_unique.split(".", 1)[1]
                    draw_ib = (
                        raw_unique.split("-")[0] if "-" in raw_unique else raw_unique
                    )
            return (
                draw_ib,
                str(getattr(model, "match_first_index", "") or ""),
                str(getattr(model, "match_index_count", "") or ""),
            )

        baseline_part_by_draw: dict[tuple, dict] = {}
        baseline_model_by_unique: dict[str, object] = {}
        for model in baseline_models:
            part = {
                "component_id": len(parts),
                "unique_str": model.unique_str,
                "lod": baseline_lod,
                "vg_offset": int(getattr(model, "vg_offset", 0) or 0),
                "vg_count": int(getattr(model, "vg_count", 0) or 0),
                "draws": [
                    {
                        "unique_str": model.unique_str,
                        "lod_level": 0,
                        "match_draw_ib": getattr(model, "match_draw_ib", ""),
                        "match_index_count": str(
                            getattr(model, "match_index_count", "") or ""
                        ),
                        "match_first_index": str(
                            getattr(model, "match_first_index", "") or ""
                        ),
                        "remap": None,
                    }
                ],
            }
            parts.append(part)
            baseline_part_by_draw[_baseline_draw_key(model)] = part
            baseline_model_by_unique[model.unique_str] = model
            component_id_dict[model.unique_str] = part["component_id"]

        # F2（t5 修复）：工作空间根，供「缺基准的 same-IB 折叠候选」判定。
        _ws_root = ""
        try:
            _ws_root_provider = getattr(GlobalConfig, "path_workspace_folder", None)
            _ws_root = (
                str(_ws_root_provider() or "").strip()
                if callable(_ws_root_provider)
                else ""
            )
        except Exception:
            _ws_root = ""

        def _lod_level_of(lod_name: str) -> int:
            try:
                return int(lod_name[3:]) if lod_name[3:].isdigit() else 1
            except (TypeError, ValueError, IndexError):
                return 1

        for model in non_baseline_models:
            lod_level = _lod_level_of(_lod_name(model.unique_str))
            # same-IB 跨 LOD：与某基准部件相同 IB/draw（且 IB 为真实 hash，空
            # 占位不算）→ 并入基准部件（component_id_dict 映射、不生成独立
            # component/draw 入口）。
            baseline_part = None
            if _baseline_draw_key(model)[0]:
                baseline_part = baseline_part_by_draw.get(_baseline_draw_key(model))
            if baseline_part is not None:
                component_id_dict[model.unique_str] = baseline_part["component_id"]
                before_fold_count = len(same_ib_bone_aliases)
                baseline_model = baseline_model_by_unique.get(
                    baseline_part["unique_str"]
                )
                if baseline_model is not None:
                    for source_id, target_id in self._build_same_ib_bone_aliases(
                        baseline_model, model
                    ).items():
                        previous = same_ib_bone_aliases.get(source_id)
                        if previous is not None and previous != target_id:
                            raise RuntimeError(
                                "[EFMI骨骼合并] same-IB 骨骼别名冲突: "
                                f"{source_id} 同时映射到 {previous} / {target_id}"
                            )
                        same_ib_bone_aliases[source_id] = target_id
                        same_ib_alias_targets_by_lod.setdefault(
                            _lod_name(model.unique_str), set()
                        ).add(target_id)
                # FC-4（C9）：被折叠 LOD 部件的自属声明段 = 运行时死段（同 IB
                # 只保留基准 EntryPoint，INI 永不注册该段）。记录供写盘断言
                # （_write_buffer_files_to_folder）与日志（prepare_merged_skeleton）。
                folded_segment_start = int(getattr(model, "vg_offset", 0) or 0)
                folded_segment_end = folded_segment_start + int(
                    getattr(model, "vg_count", 0) or 0
                )
                if folded_segment_end > folded_segment_start:
                    self._efmi_folded_dead_segments.append({
                        "unique_str": str(model.unique_str),
                        "segment": (folded_segment_start, folded_segment_end),
                    })
                continue
            # F2（t5 修复）：未折叠的非基准部件若实为 same-IB 折叠候选（与
            # 工作空间某基准共用同一游戏 draw，bare 名 = drawib-count-first =
            # 同 IB/draw 三元组），但基准未进入本次导出批次 → fail-closed 中止。
            # 同一 draw 只能有一个入口（同 hash 冲突/双写），且其声明段在折叠
            # 语义下为运行时死段：静默按独立部件导出 = t2「引用不存在骨骼」
            # 根因再入，FC-4 死段保护也因此空转。
            missing_baseline = ExportEFMI._missing_fold_baseline_unique_str(
                _ws_root, model, baseline_model_by_unique
            )
            if missing_baseline:
                raise RuntimeError(
                    f"[EFMI骨骼合并/F2] {str(getattr(model, 'unique_str', '') or '')} "
                    "是 same-IB 折叠候选（与工作空间基准 "
                    f"{missing_baseline} 共用同一游戏 draw/IB），但该基准未进入"
                    "本次导出批次——同一 draw 不允许存在两个入口（与基准入口同 "
                    "hash 冲突/双写），且其自属段在折叠语义下为运行时死段：静默"
                    "按独立部件导出会产出「引用不存在骨骼」的坏模组。请确保导出"
                    "批次包含全部 LOD0 基准部件（完整重新导入/全量导出）后重试"
                    "（fail-closed）"
                )
            # 不用“顶点数/拓扑与基准 LOD 相同”推断数据错位：游戏可以让多个
            # LOD 故意复用完全相同的网络。骨骼正确性由对应账本、独立槽位域和
            # 写盘前 BlendIndices 域校验决定，几何相同本身不是错误证据。
            # 不同 IB（或直接对应）：独立部件，挂自己的槽位段（v10 编号保证
            # 与基准/其它 LOD 不相交）、自己的绘制入口，remap 恒 None。
            part = {
                "component_id": len(parts),
                "unique_str": model.unique_str,
                "lod": _lod_name(model.unique_str),
                "vg_offset": int(getattr(model, "vg_offset", 0) or 0),
                "vg_count": int(getattr(model, "vg_count", 0) or 0),
                "draws": [
                    {
                        "unique_str": model.unique_str,
                        "lod_level": lod_level,
                        "match_draw_ib": getattr(model, "match_draw_ib", ""),
                        "match_index_count": str(
                            getattr(model, "match_index_count", "") or ""
                        ),
                        "match_first_index": str(
                            getattr(model, "match_first_index", "") or ""
                        ),
                        "remap": None,
                    }
                ],
            }
            # 槽位碰撞防线：独立部件的范围若与任一已有部件重叠，说明编号口径
            # 不一致（缓存 v9 投影/旧版未重生成）——两个组件抢占同一批槽位 =
            # LOD0/LOD1 串在一起，必须大声报错而不是输出坏 mod。
            for existing in parts:
                existing_start = existing["vg_offset"]
                existing_end = existing_start + existing["vg_count"]
                candidate_start = part["vg_offset"]
                candidate_end = candidate_start + part["vg_count"]
                if candidate_start < existing_end and existing_start < candidate_end:
                    raise RuntimeError(
                        f"[EFMI骨骼合并] {model.unique_str} 的合并骨架槽位范围 "
                        f"[{candidate_start}, {candidate_end}) 与已有部件 "
                        f"{existing['unique_str']} [{existing_start}, {existing_end}) 重叠："
                        "该子网格的 VGOffset 与其它部件冲突（编号非分段平移，可能为"
                        "旧版 v9 投影缓存或元数据未透传）。请清空骨骼合并缓存并重新"
                        "执行骨骼合并反查后导出"
                    )
            parts.append(part)
            component_id_dict[model.unique_str] = part["component_id"]

        self._efmi_merged_skeleton_bone_aliases = same_ib_bone_aliases
        self._efmi_same_ib_alias_targets_by_lod = {
            lod_name: frozenset(target_ids)
            for lod_name, target_ids in same_ib_alias_targets_by_lod.items()
        }
        # t10（方案 A4，t9 报告 §5-A4）：合并模式双套更名后，写盘缓冲的
        # BLENDINDICES 已是导出身份 e(s)（合并槽最强源组身份，非槽位号），
        # same-IB 折叠 aliases 的 source 键必须同步换算为 e(s)，否则 `_remap_`
        # 按槽位号匹配将永不命中（更名后缓冲里不存在槽位号）。
        # 单源槽 e(s)==s 恒等不变化；合并槽 e(s) 为注入（A2 断言）⇒ 无键冲突。
        if same_ib_bone_aliases:
            renamed_aliases = self._rekey_same_ib_aliases_by_export_identity(
                same_ib_bone_aliases
            )
            if renamed_aliases is not same_ib_bone_aliases:
                self._efmi_merged_skeleton_bone_aliases = renamed_aliases
                print(
                    "[EFMI骨骼合并] same-IB 折叠 aliases 已按双套导出身份"
                    f"（e(s)）重写: {len(renamed_aliases)} 项"
                )
        if same_ib_bone_aliases:
            print(
                f"[EFMI骨骼合并] same-IB 跨 LOD 折叠生成 "
                f"{len(same_ib_bone_aliases)} 个骨骼槽位重定向"
                "（目标为基准 component 连续导入槽）"
            )
            # FC-4（C9）：记录折叠别名目标（写盘断言 FC-2 的允许集合之一）。
            self._efmi_fold_alias_targets = {
                int(k): int(v) for k, v in same_ib_bone_aliases.items()
            }
        if self._efmi_folded_dead_segments:
            preview_segments = ", ".join(
                f"{item['unique_str']}[{item['segment'][0]},{item['segment'][1]})"
                for item in self._efmi_folded_dead_segments[:8]
            )
            suffix = "…" if len(self._efmi_folded_dead_segments) > 8 else ""
            print(
                "[EFMI骨骼合并] same-IB 折叠死段（运行时不注册、无人写入）: "
                f"{preview_segments}{suffix}"
            )
        # C9：绘制入口集合（绑定网格的 unique_str）。折叠部件无独立入口 →
        # 其 Blend.buf 属未绑定死文件；_write_buffer_files_to_folder 据此区分
        # FC-4 断言对象（只对绑定网格做死段零写入）并打印死段佐证。
        self._efmi_merged_draw_entries = {
            draw["unique_str"]: (part, draw)
            for part in parts
            for draw in part.get("draws", [])
        }
        # I2 可达性守卫：写盘前验证每个存活 Blend 引用槽在其出现距离必被维护者
        # 写入；违反（跨组件悬空引用）明确拒绝导出，绝不产出坏 mod。
        # S5（t20）：守卫校验键 = 写盘索引 = 导出身份 e(s)，需按 e_of 表换算
        # （t10 更名后缓冲是身份而非槽位）。
        export_identity_map = self._build_dualset_export_identity_map()
        # t25/per-mesh（v3）：守卫校验键必须是**每个模型自己的 per-mesh 身份**
        # （写盘索引 = 本组件成员身份，非全局 e(s)）——逐模型构建，跨组件引用
        # 校验落点与写盘索引一致。
        pm_ws = ""
        try:
            _pf = getattr(GlobalConfig, "path_workspace_folder", None)
            pm_ws = str(_pf() or "").strip() if callable(_pf) else ""
        except Exception:
            pm_ws = ""
        per_mesh_maps: dict[str, dict[int, int]] = {}
        if pm_ws:
            from ...common.efmi_skeleton import EFMIBoneMapBuilder
            for _m in self.submesh_model_list:
                _us = str(getattr(_m, "unique_str", "") or "").strip()
                if not _us:
                    continue
                try:
                    per_mesh_maps[_us] = (
                        EFMIBoneMapBuilder.build_per_mesh_identity_map(
                            pm_ws, _us
                        )
                    )
                except RuntimeError as exc:
                    # T6-F5（t6-F5）：FC-1 的 RuntimeError 是「槽无自属成员/表
                    # 不一致」的**预期** fail-closed 信号——I2 守卫允许按全局
                    # e_of 严格路径回退（与其自身校验键语义一致），但必须打
                    # warn 日志便于故障定位；**其它异常（数据损坏/IO 错误等）
                    # 一律上抛**，不得在守卫层静默吞掉真正的问题。
                    print(
                        f"[EFMI骨骼合并] 警告 {_us} 的 per-mesh 身份映射不可用"
                        f"（{str(exc)[:200]}），I2 可达性守卫回退全局 e_of 严格"
                        "路径（FC-2 写盘域断言仍兜底）"
                    )
                    per_mesh_maps[_us] = {}
        self._validate_merged_slot_reachability(
            parts,
            component_id_dict,
            self.submesh_model_list,
            same_ib_bone_aliases,
            export_identity_map=export_identity_map,
            per_mesh_maps=per_mesh_maps,
        )
        return parts, component_id_dict

    def _build_dualset_export_identity_map(self) -> dict[int, int]:
        """构建槽位→导出身份 e(s) 表（t10 更名语义；缓存命中零成本）。"""
        try:
            _workspace_folder = getattr(GlobalConfig, "path_workspace_folder", None)
            workspace_root = (
                str(_workspace_folder() or "").strip()
                if callable(_workspace_folder)
                else ""
            )
        except Exception:
            workspace_root = ""
        if not workspace_root:
            return {}
        from ...common.efmi_skeleton import EFMIBoneMapBuilder
        try:
            table = EFMIBoneMapBuilder.get_dualset_export_table_cached(workspace_root)
        except RuntimeError:
            return {}
        return {
            int(slot): int(row["export_identity"])
            for slot, row in table.items()
        }

    def _add_merged_skeleton_section(self, ini_builder, command_lists_section=None):
        """生成 EFMI 骨骼合并（Merged Skeleton）INI 段（对齐 EFMI 1.4.1 运行时契约）。

        单池语义（2026-08-28 改版，对齐参考插件 mod.ini.j2）：所有逻辑部件
        （v10：每个子网格/每个 LOD 版本各为一个组件，槽位段分段平移不相交）
        共用一套 MergedSkeleton 配置——一个 Constants 块（$component_count/
        $bones_count/$max_instance_count/$merged_skeleton_initialized）
        + 5 个 Pool（VertexGroupOffsets/Counts/LodRemaps/Instance_UpdateFrame/
        Instance_LodLevel）+ Pool_ObjectSpatialIdentity + 一个
        ResourceMergedSkeletonDataRW + CommandList_MergedSkeleton_ConnectComponent
        （守卫初始化 + 绑定 pools + AttachComponent + ElementFormat 16 位）+
        CommandListInitializeMergedSkeleton（逐部件写 vg_offset/vg_count，
        LodRemaps 全 null——v10 无跨 LOD full→lod remap）。

        多 LOD 语义（v10，撤销 v9 共享槽位投影）：每个 LOD 的
        槽位段相互独立（LOD0 0..max0、LOD1 base 起），LOD1 绘制入口挂**自己**
        的 component 槽位段；运行时 MergedSkeleton_Apply 把当前 LOD draw 自己的
        矩阵写入自己的槽位段（remap=null 恒等路径），LOD0/LOD1 矩阵不共享
        不混用。v9 投影实测 LOD1 爆炸 = LOD1 顶点组引用 LOD0 槽位 + 运行时对
        同一 component 每帧只导入一次骨骼（仅更优 $lod_level 覆盖），同帧先
        LOD0 后 LOD1 时 LOD1 网格读到 LOD0 矩阵。
        相同 IB 的 LOD 绘制（脸部件）不生成第二入口（参考插件 same-IB 处理）。
        运行时只有一根骨架缓冲、一套 Resource/Pool/BonesCount/粘合层，
        不再逐 LOD 切换 EFMIv1 命名空间资源。
        bones_count = max(vg_offset+vg_count)。

        另向 command_lists_section 追加官方绘制管线粘合层（单套）
        [CommandList_Component_DrawInstances]：命名空间配置赋值（component_count/
        bones_count/instance_count——运行时只读 EFMIv1 命名空间内的值，漏赋
        bones_count 会让合并骨骼按 0 根计算）+ Component_ReadConfig + 空间实例
        识别 + ConnectComponent 回调挂载 + run Component_DrawInstances（运行时
        接管逐实例迭代与 MergedSkeleton_Apply）。按用户要求不做 DRAW_TYPE
        通道门控，全通道生效。
        """
        components = self.merged_skeleton_components
        if not components:
            return

        component_count = len(components)
        # 骨骼总数口径：全池 max(vg_offset + vg_count)。部件 vg_offset 为
        # 全绑定空间槽位（v10：各 LOD 分段平移后的不相交槽位段），合并骨架
        # 缓冲与逐实例区域数学必须覆盖组件声明的最大槽位，否则越界空转。
        bones_count = max(comp["vg_offset"] + comp["vg_count"] for comp in components)
        max_instance_count = 8  # 与参考插件 cfg.max_instance_count 一致

        section = M_IniSection(M_SectionType.MergedSkeleton)
        constants_section = M_IniSection(M_SectionType.Constants)
        constants_section.SectionName = "Constants"
        constants_section.append(f"global $component_count = {component_count}")
        constants_section.append(f"global $bones_count = {bones_count}")
        constants_section.append(f"global $max_instance_count = {max_instance_count}")
        constants_section.append("global $merged_skeleton_initialized = 0")
        constants_section.new_line()

        section.append("[Pool_MergedSkeleton_Component_VertexGroupOffsets]")
        section.append(f"pool_size = $component_count")
        section.new_line()

        section.append("[Pool_MergedSkeleton_Component_VertexGroupCounts]")
        section.append(f"pool_size = $component_count")
        section.new_line()

        section.append("[Pool_MergedSkeleton_Component_LodRemaps]")
        section.append(f"pool_size = $component_count * $\\EFMIv1\\cfg_ms_max_lod_level_count")
        section.new_line()

        section.append("[Pool_MergedSkeleton_Instance_UpdateFrame]")
        section.append(f"pool_size = $component_count * $max_instance_count")
        section.new_line()

        section.append("[Pool_MergedSkeleton_Instance_LodLevel]")
        section.append(f"pool_size = $component_count * $max_instance_count")
        section.new_line()

        # 空间实例识别输入池（官方管线必需：MergedSkeleton_Apply 经
        # PoolSpatialIdentity_SpatialIds[$draw_call_instance_id] 取实例 id，
        # 该池只能由 SpatialIdentity_IdentifyComponentInstances 以此池为输入填充）
        section.append("[Pool_ObjectSpatialIdentity]")
        section.append(f"pool_size = $max_instance_count * $\\EFMIv1\\cfg_spatial_instance_load_ratio")
        section.append("pool_index_type = spatial")
        section.append("pool_spatial_radius = $\\EFMIv1\\cfg_spatial_base_radius")
        section.append("pool_expiration_timeout_frames = $\\EFMIv1\\cfg_spatial_expiration_frames")
        section.append("pool_expiration_reset_elements = $\\EFMIv1\\cfg_spatial_expiration_reset")
        section.append("pool_expiration_refresh_on_read = $\\EFMIv1\\cfg_spatial_expiration_read_refresh")
        section.append("pool_variable_default_value = $\\EFMIv1\\cfg_spatial_detault_value")
        section.new_line()

        section.append("[ResourceMergedSkeletonDataRW]")
        section.append("type = RWBuffer")
        section.append("format = R32G32B32A32_FLOAT")
        section.append(
            f"array = ($\\EFMIv1\\cfg_ms_implicit_bones_count + $\\EFMIv1\\cfg_ms_skeletons_count "
            f"* $bones_count * $max_instance_count) * $\\EFMIv1\\cfg_ms_bone_entry_size"
        )
        section.new_line()

        section.append("[CommandList_MergedSkeleton_ConnectComponent]")
        section.append("if !$merged_skeleton_initialized")
        section.append("    $merged_skeleton_initialized = 1")
        section.append("    run = CommandListInitializeMergedSkeleton")
        section.append("endif")
        section.append("Pool\\EFMIv1\\Input_MergedSkeleton_Component_VertexGroupOffsets = ref Pool_MergedSkeleton_Component_VertexGroupOffsets")
        section.append("Pool\\EFMIv1\\Input_MergedSkeleton_Component_VertexGroupCounts = ref Pool_MergedSkeleton_Component_VertexGroupCounts")
        section.append("Pool\\EFMIv1\\Input_MergedSkeleton_Component_LodRemaps = ref Pool_MergedSkeleton_Component_LodRemaps")
        section.append("Pool\\EFMIv1\\Input_MergedSkeleton_Instance_UpdateFrame = ref Pool_MergedSkeleton_Instance_UpdateFrame")
        section.append("Pool\\EFMIv1\\Input_MergedSkeleton_Instance_LodLevel = ref Pool_MergedSkeleton_Instance_LodLevel")
        section.append("Resource\\EFMIv1\\Output_MergedSkeleton = ref ResourceMergedSkeletonDataRW")
        section.append("run = CommandList\\EFMIv1\\MergedSkeleton_AttachComponent")
        section.append("; BLENDINDICES layouts after merged-skeleton widening")
        merged_unique_strs = {
            draw["unique_str"]
            for comp in components
            for draw in comp.get("draws", [])
        }
        lod_submesh_models = [
            model for model in self.submesh_model_list
            if model.unique_str in merged_unique_strs
        ]
        blend_layouts = self._validated_blendindices_layouts(
            lod_submesh_models,
            "[EFMI骨骼合并] 全池",
        )
        for semantic_index, element_format, extract_slot in blend_layouts:
            section.append(
                f"{extract_slot}->ElementFormat(BLENDINDICES, {semantic_index}) = "
                f"{element_format}"
            )
        section.new_line()

        section.append("[CommandListInitializeMergedSkeleton]")
        section.append("Resource\\EFMIv1\\OutputMergedSkeleton_Template = ref ResourceMergedSkeletonDataRW")
        section.append("run = CommandList\\EFMIv1\\InitializeMergedSkeleton")
        section.append("local $lod_level_count = $\\EFMIv1\\cfg_ms_max_lod_level_count")
        section.append("local $component_id")
        # C10（t3 设计 P7）：注册前断言——折叠家族 LOD1 死段永不注册为独立组件。
        # 折叠部件已并入基准 component（component_id_dict 映射），本列表只含
        # 绑定组件；此断言是防御性兜底：若未来任何路径把死段当独立组件注册
        # （运行时无人 attach → 引用即「不存在骨骼」），在此大声失败。
        dead_segments_for_ini = getattr(self, "_efmi_folded_dead_segments", []) or []
        for comp in components:
            comp_start = int(comp["vg_offset"])
            comp_end = comp_start + int(comp["vg_count"])
            for dead_item in dead_segments_for_ini:
                dead_start, dead_end = int(dead_item["segment"][0]), int(dead_item["segment"][1])
                if comp_start < dead_end and dead_start < comp_end:
                    raise RuntimeError(
                        f"[EFMI骨骼合并/C10] 折叠死段 "
                        f"{dead_item['unique_str']}[{dead_start},{dead_end}) "
                        f"被注册为独立组件 {comp['unique_str']} "
                        f"[{comp_start},{comp_end})：运行时无人写入该段，"
                        "中止导出（fail-closed）"
                    )
        for component_id, comp in enumerate(components):
            section.append(f"$component_id = {component_id}")
            section.append(f"$Pool_MergedSkeleton_Component_VertexGroupOffsets[$component_id] = {comp['vg_offset']}")
            section.append(f"$Pool_MergedSkeleton_Component_VertexGroupCounts[$component_id] = {comp['vg_count']}")
            # LodRemaps：+0 恒 null（$lod_level=0 恒等路径）；其余槽位显式 null。
            # v10 各 LOD 槽位段独立（无 cross-LOD full→lod BlendRemap），
            # 任何 $lod_level 都走恒等路径，防残留误用永不写入的槽位。
            for level in range(int(_EFMI_MAX_LOD_LEVEL_COUNT)):
                section.append(
                    f"Pool_MergedSkeleton_Component_LodRemaps[$component_id*$lod_level_count+{level}] = null"
                )
        section.new_line()

        # 官方绘制管线粘合层（单套，所有 LOD 组件共用）：运行时
        # Component_DrawInstances 逐实例迭代、每实例 MergedSkeleton_Apply 后
        # 才回调组件绘制（CommandList_Draw_<部件前缀>）。identification_min_components
        # 默认 4 是按整角色设定的；按本池组件数取 min(component_count, 4)。
        if command_lists_section is not None:
            command_lists_section.append("[CommandList_Component_DrawInstances]")
            command_lists_section.append("handling = skip")
            command_lists_section.append(f"$\\EFMIv1\\component_count = $component_count")
            command_lists_section.append(f"$\\EFMIv1\\bones_count = $bones_count")
            command_lists_section.append(f"$\\EFMIv1\\instance_count = $max_instance_count")
            command_lists_section.append("run = CommandList\\EFMIv1\\Object_ReadConfig")
            # 注意：不再写 $\EFMIv1\lod_level = $lod_level 裸版桥接——框架
            # API.ini 的 `global $lod_level = 0` 在 namespace=EFMIv1 下声明的是
            # $\EFMIv1\lod_level 而非裸版全局；裸版 $lod_level 在本导出中从未
            # 被任何 EntryPoint 赋值（v10/v13 每个子网格 = 独立 component，
            # 单绘制入口），此前该行在 3Dmigoto 加载时必然报
            # "Variable not recognized: lod_level"，且被丢弃后 $\EFMIv1\lod_level
            # 恒等于框架默认 0——无论删除与否运行时语义一致，故整段移除。
            command_lists_section.append("$\\EFMIv1\\custom_mesh_scale = 1.00")
            command_lists_section.append(
                "$\\EFMIv1\\identification_min_components = " + str(min(component_count, 4))
            )
            command_lists_section.append("run = CommandList\\EFMIv1\\Component_ReadConfig")
            command_lists_section.append(
                "Pool\\EFMIv1\\Input_ObjectSpatialIdentity = ref Pool_ObjectSpatialIdentity"
            )
            command_lists_section.append(
                "run = CommandList\\EFMIv1\\SpatialIdentity_IdentifyComponentInstances"
            )
            command_lists_section.append(
                "CommandList\\EFMIv1\\Callback_MergedSkeleton_ConnectComponent = "
                "ref CommandList_MergedSkeleton_ConnectComponent"
            )
            command_lists_section.append("run = CommandList\\EFMIv1\\Component_DrawInstances")
            command_lists_section.new_line()

        ini_builder.append_section(section)
        ini_builder.append_section(constants_section)


    def _append_submesh_draw_bindings(self, section, submesh_model, drawib_model):
        """子网格绘制绑定：OverrideTextures + ib/vb 缓冲绑定 + 贴图槽位绑定。

        骨骼合并模式下作为 CommandList_Draw_<部件前缀> 的回调主体（运行时逐实例
        MergedSkeleton_Apply 换绑合并骨架后调用）。非合并模式的 TextureOverrideIB
        段内仍保留一份内联实现（输出内容相同），未收敛以隔离回归风险。
        """
        section.append("run = CommandList\\EFMIv1\\OverrideTextures")

        ib_resource_name = "Resource_" + submesh_model.unique_str.replace("-","_") + "_Index"
        section.append("ib = " + ib_resource_name)

        for category in submesh_model.category_buffer_dict.keys():
            category_slot = submesh_model.d3d11_game_type.CategoryExtractSlotDict.get(category,"unknown_slot")
            category_resource_name = "Resource_" + submesh_model.unique_str.replace("-","_")  + "_" + category
            section.append(category_slot + " = " + category_resource_name)

        unique_str = submesh_model.unique_str
        section.append("vb3 = Resource_" + unique_str.replace('-', '_') + "_Position")

        if not GlobalProterties.forbid_auto_texture_ini() and drawib_model is not None:
            texture_markup_info_list = drawib_model.get_submesh_texture_markup_info_list(submesh_model)
            if GlobalProterties.use_rabbitfx_slot():
                for texture_markup_info in texture_markup_info_list:
                    if not M_IniHelper.is_slot_binding_mark_type(getattr(texture_markup_info, "mark_type", "")):
                        continue
                    if texture_markup_info.mark_name == "DiffuseMap":
                        section.append("Resource\\RabbitFx\\Diffuse = ref " + texture_markup_info.get_resource_name())
                    elif texture_markup_info.mark_name == "LightMap":
                        section.append("Resource\\RabbitFx\\LightMap = ref " + texture_markup_info.get_resource_name())
                    elif texture_markup_info.mark_name == "NormalMap":
                        section.append("Resource\\RabbitFx\\NormalMap = ref " + texture_markup_info.get_resource_name())

                section.append("run = CommandList\\RabbitFx\\SetTextures")

                for texture_markup_info in texture_markup_info_list:
                    if not M_IniHelper.is_slot_binding_mark_type(getattr(texture_markup_info, "mark_type", "")):
                        continue
                    if texture_markup_info.mark_name in ["DiffuseMap", "LightMap", "NormalMap"]:
                        pass
                    else:
                        slot = texture_markup_info.mark_slot
                        if slot and not slot.lower().startswith("ps-t"):
                            num_match = re.search(r'\d+', slot)
                            if num_match:
                                slot = "ps-t" + num_match.group()
                            else:
                                slot = "ps-t" + slot
                        section.append(slot + " = " + texture_markup_info.get_resource_name())
            else:
                for texture_markup_info in texture_markup_info_list:
                    if not M_IniHelper.is_slot_binding_mark_type(getattr(texture_markup_info, "mark_type", "")):
                        continue
                    section.append(texture_markup_info.mark_slot + " = " + texture_markup_info.get_resource_name())

    def prepare_merged_skeleton(self):
        """收集合并骨架逻辑部件（幂等，可在缓冲区生成前调用）。

        generate_buffer_files 需要 same-IB 重定向与绘制入口数据，
        generate_ini_file 需要部件池——两者都必须先于缓冲/INI 生成执行完。
        """
        if getattr(self, "_efmi_merged_skeleton_prepared", False):
            return
        self.merged_skeleton_components, self.merged_skeleton_component_id_dict = (
            self._get_merged_skeleton_component_info()
        )
        self.has_merged_skeleton = len(self.merged_skeleton_components) > 0
        self._efmi_merged_skeleton_prepared = True
        if self.has_merged_skeleton:
            total_bones = max(
                (c["vg_offset"] + c["vg_count"] for c in self.merged_skeleton_components),
                default=0,
            )
            print(
                f"[EFMI骨骼合并] 合并骨架: {len(self.merged_skeleton_components)} 个逻辑部件, "
                f"单池共 {total_bones} 槽（跨 LOD 共用同一骨架缓冲）"
            )

    def generate_ini_file(self):
        ini_builder = M_IniBuilder()

        # EFMI 骨骼合并（Merged Skeleton）部件信息（export() 已提前收集；
        # 单独调用时兜底收集）
        self.prepare_merged_skeleton()

        drawib_drawibmodel_dict = {
            drawib_model.draw_ib: drawib_model
            for drawib_model in self.drawib_model_list
        }

        if self.has_cross_ib:
            self._add_cross_ib_present_section(ini_builder)
            self._add_cross_ib_resource_id_sections(ini_builder)

        M_IniHelper.generate_hash_style_texture_ini(
            ini_builder=ini_builder,
            drawib_drawibmodel_dict=drawib_drawibmodel_dict,
        )
        M_IniHelper.generate_shared_slot_style_texture_ini(
            ini_builder=ini_builder,
            drawib_drawibmodel_dict=drawib_drawibmodel_dict,
        )

        self._integrate_object_swap_ini_hook(ini_builder)

        texture_override_ib_section = M_IniSection(M_SectionType.TextureOverrideIB)

        # EFMI 骨骼合并：官方运行时架构的组件绘制回调段（CommandList_Draw_<部件前缀>）
        # 与粘合层（CommandList_Component_DrawInstances，在 _add_merged_skeleton_section 追加）
        merged_command_lists = M_IniSection(M_SectionType.CommandList)

        for submesh_model in self.submesh_model_list:
            drawib_model = drawib_drawibmodel_dict.get(submesh_model.match_draw_ib)

            current_ib_key = self._get_submesh_ib_key(submesh_model)

            is_source_ib = current_ib_key in self.cross_ib_info_dict
            source_ib_list_for_target = self.cross_ib_target_info.get(current_ib_key, [])
            is_target_ib = len(source_ib_list_for_target) > 0

            if self.cross_ib_match_mode == 'INDEX_COUNT':
                current_identifier = submesh_model.match_index_count
            else:
                current_identifier = submesh_model.match_draw_ib

            # ===== EFMI 骨骼合并组件：EntryPoint + 运行时回调绘制（官方 1.4.1 架构）=====
            # 按用户要求不做 DRAW_TYPE 通道门控：所有通道均生效。
            # 合并侧发射按「逻辑部件的绘制入口」而非逐子网格：同 IB 跨 LOD 部件
            # 只挂基准入口（参考插件 same-IB 处理），其 LOD 版本不生成第二入口。
            if self.has_merged_skeleton:
                merged_draw_entries = getattr(self, "_efmi_merged_draw_entries", None)
                if merged_draw_entries is None:
                    merged_draw_entries = {}
                    for part in self.merged_skeleton_components:
                        for draw in part.get("draws", []):
                            merged_draw_entries[draw["unique_str"]] = (part, draw)
                    self._efmi_merged_draw_entries = merged_draw_entries
                draw_entry = merged_draw_entries.get(submesh_model.unique_str)
                merged_component_id = draw_entry[0]["component_id"] if draw_entry else None
            else:
                merged_component_id = None
            if merged_component_id is not None:
                part, draw_entry_info = draw_entry
                # 段名用部件前缀（unique_str），与 Resource_<前缀>_* 命名约定一致，
                # 直接能看出是哪个部件；数字 component_id 为全局值（所有 LOD 共用
                # 一套粘合层/池，见 _add_merged_skeleton_section）。
                component_prefix = submesh_model.unique_str.replace("-", "_")
                entrypoint_section_name = "TextureOverride_EntryPoint_" + component_prefix
                draw_command_name = "CommandList_Draw_" + component_prefix
                texture_override_ib_section.append("[" + entrypoint_section_name + "]")
                texture_override_ib_section.append("hash = " + str(draw_entry_info.get("match_draw_ib", submesh_model.match_draw_ib)))
                texture_override_ib_section.append("match_first_index = " + str(draw_entry_info.get("match_first_index", submesh_model.match_first_index)))
                texture_override_ib_section.append("match_index_count = " + str(draw_entry_info.get("match_index_count", submesh_model.match_index_count)))
                # 原始绘制压制直接放 EntryPoint（本机所有可用 mod 的实证写法）：
                # 嵌套 CommandList 内的 handling=skip 在部分 3Dmigoto 分支不一定生效，
                # 粘合层里仍保留一份作为双保险。
                texture_override_ib_section.append("handling = skip")
                texture_override_ib_section.append(f"$\\EFMIv1\\component_id = {merged_component_id}")
                # 不再写裸版 $lod_level：v10/v13 下每个 component 恒为单绘制入口
                # （len(draws)==1，含 same-IB 折叠，draws 不追加第二入口），此写入
                # 永不触发；且裸版 $lod_level 无全局声明（框架 API.ini 的声明在
                # namespace=EFMIv1 名下），一旦触发同样会报未识别变量。各 LOD 的
                # 矩阵归属已由独立 component 槽位段（段平移互不相交）保证，运行时
                # $\EFMIv1\lod_level 恒为框架默认 0 即可，无需逐入口传递。
                texture_override_ib_section.append("$\\EFMIv1\\gpu_posed = 1")
                texture_override_ib_section.append(
                    "CommandList\\EFMIv1\\Callback_Component_DrawCustom = ref " + draw_command_name
                )
                texture_override_ib_section.append(
                    "run = CommandList_Component_DrawInstances"
                )
                if len(self.blueprint_model.keyname_mkey_dict.keys()) != 0:
                    texture_override_ib_section.append("$active0 = 1")
                    if GlobalProterties.generate_branch_mod_gui():
                        texture_override_ib_section.append("$ActiveCharacter = 1")
                texture_override_ib_section.new_line()

                if (is_source_ib or is_target_ib) and self.has_cross_ib:
                    # 跨 IB 重定向管线（录制骨骼/R redirect）与合并骨架互斥：
                    # 合并骨架下骨骼已统一，组件按自身 IB 直接绘制即可。
                    print(
                        f"[EFMI骨骼合并] 警告: {submesh_model.unique_str} 配置了跨 IB；"
                        "合并骨架模式下跨 IB 重定向不适用，已按自身 IB 直接绘制"
                    )

                # 组件绘制回调主体：运行时逐实例 Apply（换绑合并骨架）后调用
                merged_command_lists.append("[" + draw_command_name + "]")
                self._append_submesh_draw_bindings(
                    merged_command_lists, submesh_model, drawib_model
                )
                self._append_drawindexed_instanced_with_shader_replace(
                    merged_command_lists,
                    submesh_model.drawcall_model_list,
                    None,
                )
                merged_command_lists.new_line()
                continue

            if (
                self.has_merged_skeleton
                and submesh_model.unique_str in self.merged_skeleton_component_id_dict
            ):
                # same-IB 跨 LOD 部件（已并入基准 draw）：不生成任何入口——
                # 参考插件对同 IB 部件的处理（单入口、无 LOD 检测），
                # 否则与基准入口同 hash 冲突/双写。
                continue

            texture_override_ib_section.append("[TextureOverride_" + submesh_model.unique_str.replace("-","_") + "]")
            texture_override_ib_section.append("hash = " + submesh_model.match_draw_ib)
            texture_override_ib_section.append("match_first_index = " + submesh_model.match_first_index)
            texture_override_ib_section.append("match_index_count = " + submesh_model.match_index_count)
            texture_override_ib_section.append("handling = skip")

            # EFMI 骨骼合并升宽配套（非组件兜底）：合并组件已在上方走 EntryPoint 分支，
            # 这里只服务"升宽但无反查数据"的子网格——仅输出 ElementFormat 行（数据侧升宽，无运行时挂载）。
            if getattr(submesh_model, "blendindices_widened", False):
                for semantic_index, element_format, extract_slot in (
                    self._validated_blendindices_layouts(
                        [submesh_model],
                        f"[EFMI骨骼合并] {submesh_model.unique_str}",
                    )
                ):
                    texture_override_ib_section.append(
                        f"{extract_slot}->ElementFormat(BLENDINDICES, {semantic_index}) = "
                        f"{element_format}"
                    )

            if is_target_ib:
                texture_override_ib_section.append("analyse_options = deferred_ctx_immediate dump_rt dump_cb dump_vb dump_ib buf txt dds dump_tex dds symlink")

            texture_override_ib_section.append("run = CommandList\\EFMIv1\\OverrideTextures")

            ib_resource_name = "Resource_" + submesh_model.unique_str.replace("-","_") + "_Index"
            texture_override_ib_section.append("ib = " + ib_resource_name)

            for category in submesh_model.category_buffer_dict.keys():
                category_slot = submesh_model.d3d11_game_type.CategoryExtractSlotDict.get(category,"unknown_slot")
                category_resource_name = "Resource_" + submesh_model.unique_str.replace("-","_")  + "_" + category
                texture_override_ib_section.append(category_slot + " = " + category_resource_name)

            unique_str = submesh_model.unique_str
            texture_override_ib_section.append("vb3 = Resource_" + unique_str.replace('-', '_') + "_Position")

            if not GlobalProterties.forbid_auto_texture_ini() and drawib_model is not None:
                texture_markup_info_list = drawib_model.get_submesh_texture_markup_info_list(submesh_model)
                if GlobalProterties.use_rabbitfx_slot():
                    for texture_markup_info in texture_markup_info_list:
                        if not M_IniHelper.is_slot_binding_mark_type(getattr(texture_markup_info, "mark_type", "")):
                            continue
                        if texture_markup_info.mark_name == "DiffuseMap":
                            texture_override_ib_section.append("Resource\\RabbitFx\\Diffuse = ref " + texture_markup_info.get_resource_name())
                        elif texture_markup_info.mark_name == "LightMap":
                            texture_override_ib_section.append("Resource\\RabbitFx\\LightMap = ref " + texture_markup_info.get_resource_name())
                        elif texture_markup_info.mark_name == "NormalMap":
                            texture_override_ib_section.append("Resource\\RabbitFx\\NormalMap = ref " + texture_markup_info.get_resource_name())
                    
                    texture_override_ib_section.append("run = CommandList\\RabbitFx\\SetTextures")
                    
                    for texture_markup_info in texture_markup_info_list:
                        if not M_IniHelper.is_slot_binding_mark_type(getattr(texture_markup_info, "mark_type", "")):
                            continue
                        if texture_markup_info.mark_name in ["DiffuseMap", "LightMap", "NormalMap"]:
                            pass
                        else:
                            slot = texture_markup_info.mark_slot
                            if slot and not slot.lower().startswith("ps-t"):
                                num_match = re.search(r'\d+', slot)
                                if num_match:
                                    slot = "ps-t" + num_match.group()
                                else:
                                    slot = "ps-t" + slot
                            texture_override_ib_section.append(slot + " = " + texture_markup_info.get_resource_name())
                else:
                    for texture_markup_info in texture_markup_info_list:
                        if not M_IniHelper.is_slot_binding_mark_type(getattr(texture_markup_info, "mark_type", "")):
                            continue
                        texture_override_ib_section.append(texture_markup_info.mark_slot + " = " + texture_markup_info.get_resource_name())

            is_both_source_and_target = is_source_ib and is_target_ib and self.has_cross_ib

            if is_both_source_and_target:
                cross_ib_drawcalls, non_cross_ib_drawcalls = self._split_drawcalls_by_cross_ib(
                    submesh_model.drawcall_model_list,
                    source_ib_key=current_ib_key
                )

                target_ib_keys = self.cross_ib_source_to_target_dict.get(current_ib_key, [])
                grouped_source_drawcalls = self._group_drawcalls_by_cross_ib_target(
                    cross_ib_drawcalls, current_ib_key, target_ib_keys
                )

                for (target_ib_key, vb_condition), objects in grouped_source_drawcalls.items():
                    if not objects:
                        continue

                    texture_override_ib_section.append(";跨 iB 区域")
                    self._append_source_cross_ib_replay(
                        texture_override_ib_section,
                        vb_condition,
                        objects,
                        current_identifier,
                    )

                texture_override_ib_section.append(";不需要跨 Ib 的物体引用")

                if non_cross_ib_drawcalls:
                    self._append_drawindexed_instanced_with_shader_replace(
                        texture_override_ib_section,
                        non_cross_ib_drawcalls,
                        None,
                    )

                if is_target_ib and source_ib_list_for_target:
                    self._append_target_cross_ib_blocks(
                        texture_override_ib_section, source_ib_list_for_target, current_ib_key
                    )

                texture_override_ib_section.append("")
                texture_override_ib_section.append("post vs-cb1 = null")
                texture_override_ib_section.append("post vs-cb2 = null")
                texture_override_ib_section.append("post vs-t0 = null")
                texture_override_ib_section.append("post cs-t2 = null")

            elif is_source_ib and self.has_cross_ib:
                target_ib_keys = self.cross_ib_source_to_target_dict.get(current_ib_key, [])
                target_ib_key = target_ib_keys[0] if target_ib_keys else None
                cross_ib_lines = self._generate_cross_ib_block_for_source(
                    current_identifier, submesh_model.drawcall_model_list,
                    source_ib_key=current_ib_key, target_ib_key=target_ib_key
                )
                for line in cross_ib_lines:
                    texture_override_ib_section.append(line)

            elif is_target_ib and self.has_cross_ib and source_ib_list_for_target:
                all_target_drawcalls = submesh_model.drawcall_model_list
                if all_target_drawcalls:
                    self._append_drawindexed_instanced_with_shader_replace(
                        texture_override_ib_section,
                        all_target_drawcalls,
                        None,
                    )

                self._append_target_cross_ib_blocks(
                    texture_override_ib_section, source_ib_list_for_target, current_ib_key
                )

                texture_override_ib_section.append("")
                texture_override_ib_section.append("post vs-cb1 = null")
                texture_override_ib_section.append("post vs-cb2 = null")
                texture_override_ib_section.append("post vs-t0 = null")
                texture_override_ib_section.append("post cs-t2 = null")

            else:
                self._append_drawindexed_instanced_with_shader_replace(
                    texture_override_ib_section,
                    submesh_model.drawcall_model_list,
                    None,
                )

            if len(self.blueprint_model.keyname_mkey_dict.keys()) != 0:
                texture_override_ib_section.append("$active0 = 1")
                if GlobalProterties.generate_branch_mod_gui():
                    texture_override_ib_section.append("$ActiveCharacter = 1")

            texture_override_ib_section.new_line()

        ini_builder.append_section(texture_override_ib_section)
        if self.has_merged_skeleton:
            ini_builder.append_section(merged_command_lists)

        resource_buffer_section = M_IniSection(M_SectionType.ResourceBuffer)
        buffer_folder_name = BlueprintExportHelper.get_current_buffer_folder_name()
        for submesh_model in self.submesh_model_list:
            ib_resource_name = "Resource_" + submesh_model.unique_str.replace("-","_") + "_Index"
            resource_buffer_section.append("[" + ib_resource_name + "]")
            resource_buffer_section.append("type = Buffer")
            resource_buffer_section.append("format = DXGI_FORMAT_R32_UINT")
            ib_name = getattr(submesh_model, "workspace_unique_str", "") or submesh_model.unique_str
            resource_buffer_section.append("filename = " + buffer_folder_name + "\\" + ib_name + "-Index.buf")
            resource_buffer_section.new_line()

            for category in submesh_model.category_buffer_dict.keys():
                category_resource_name = "Resource_" + submesh_model.unique_str.replace("-","_")  + "_" + category
                stride = submesh_model.d3d11_game_type.CategoryStrideDict.get(category,0)
                resource_buffer_section.append("[" + category_resource_name + "]")
                resource_buffer_section.append("type = Buffer")
                resource_buffer_section.append("stride = " + str(stride))
                resource_buffer_section.append("filename = " + buffer_folder_name + "\\" + submesh_model.unique_str + "-" + category + ".buf")
                resource_buffer_section.new_line()

        # 合并骨架 full→lod BlendRemap 资源（R16_UINT；参考插件同款声明）
        if getattr(self, "has_merged_skeleton", False):
            for part in getattr(self, "merged_skeleton_components", []) or []:
                for draw in part.get("draws", []):
                    if not draw.get("remap"):
                        continue
                    remap_resource_name = (
                        "Resource_" + draw["unique_str"].replace("-", "_") + "_BlendRemap"
                    )
                    resource_buffer_section.append("[" + remap_resource_name + "]")
                    resource_buffer_section.append("type = Buffer")
                    resource_buffer_section.append("format = R16_UINT")
                    resource_buffer_section.append("stride = 2")
                    resource_buffer_section.append(
                        "filename = " + buffer_folder_name + "\\" + draw["unique_str"] + "-BlendRemap.buf"
                    )
                    resource_buffer_section.new_line()

        if not GlobalProterties.forbid_auto_texture_ini():
            resource_texture_section = M_IniSection(M_SectionType.ResourceTexture)
            appended_resource_names = set()
            for drawib_model in self.drawib_model_list:
                for submesh_model in drawib_model.submesh_model_list:
                    for texture_markup_info in drawib_model.get_submesh_texture_markup_info_list(submesh_model):
                        if getattr(texture_markup_info, "mark_type", "") != "Slot":
                            continue
                        resource_name = texture_markup_info.get_resource_name()
                        if resource_name in appended_resource_names:
                            continue
                        appended_resource_names.add(resource_name)
                        resource_texture_section.append("[" + texture_markup_info.get_resource_name() + "]")
                        resource_texture_section.append("filename = Textures/" + texture_markup_info.mark_filename)
                        resource_texture_section.new_line()
            ini_builder.append_section(resource_texture_section)

        ini_builder.append_section(resource_buffer_section)

        for drawib_model in self.drawib_model_list:
            M_IniHelper.move_slot_style_textures(draw_ib_model=drawib_model)

        GlobalKeyCountHelper.generated_mod_number = len(self.drawib_model_list)
        M_IniHelper.add_branch_key_sections(
            ini_builder=ini_builder,
            key_name_mkey_dict=self.blueprint_model.keyname_mkey_dict,
        )
        M_IniHelperGUI.add_branch_mod_gui_section(
            ini_builder=ini_builder,
            key_name_mkey_dict=self.blueprint_model.keyname_mkey_dict,
        )

        if self.has_shader_replace:
            M_IniHelper.add_shader_replace_sections(
                ini_builder=ini_builder,
                shader_replace_info_list=self.shader_replace_info_list,
                shader_replace_object_names=self.shader_replace_object_names,
                draw_call_models=self.blueprint_model.ordered_draw_obj_data_model_list,
                mod_export_path=GlobalConfig.path_generate_mod_folder(),
                use_instanced_draw=True,
                shader_replace_object_info_map=self.shader_replace_object_info_map,
                draw_call_offset_map=M_IniHelper.build_draw_call_offset_map(self.drawib_model_list),
            )

        # EFMI 骨骼合并（Merged Skeleton）段：在保存前追加（对齐 EFMI 1.4.1 运行时契约：
        # 命名空间配置 + 空间实例识别 + ConnectComponent 回调挂载，绘制走官方逐实例管线）
        if self.has_merged_skeleton:
            self._add_merged_skeleton_section(ini_builder, merged_command_lists)

        ini_filepath = os.path.join(GlobalConfig.path_generate_mod_folder(), GlobalConfig.get_workspace_name() + ".ini")
        ini_builder.save_to_file(ini_filepath)

        if self.has_cross_ib:
            self._copy_cross_ib_hlsl_files()

    def _append_target_cross_ib_blocks(self, section, source_ib_list_for_target, current_ib_key):
        for source_ib_key in source_ib_list_for_target:
            if self.cross_ib_match_mode == 'INDEX_COUNT':
                source_identifier = source_ib_key.replace('indexcount_', '') if source_ib_key.startswith('indexcount_') else source_ib_key.split("_")[0]
            else:
                source_hash = source_ib_key.split("_")[0]
                source_identifier = source_hash

            source_submesh = self._find_source_submesh_by_ib_key(source_ib_key)
            source_drawib_model = self._find_source_drawib_by_ib_key(source_ib_key)

            if not source_submesh or not source_drawib_model:
                continue

            cross_drawcalls, _ = self._split_drawcalls_by_cross_ib(
                source_submesh.drawcall_model_list,
                source_ib_key=source_ib_key,
                target_ib_key=current_ib_key
            )

            if not cross_drawcalls:
                continue

            grouped_cross_drawcalls = {}
            for drawcall_model in cross_drawcalls:
                obj_name = drawcall_model.obj_name if hasattr(drawcall_model, 'obj_name') else str(drawcall_model)
                vb_condition_target = self._get_vb_condition_for_object(obj_name, source_ib_key, current_ib_key, 'target')
                if vb_condition_target not in grouped_cross_drawcalls:
                    grouped_cross_drawcalls[vb_condition_target] = []
                grouped_cross_drawcalls[vb_condition_target].append(drawcall_model)

            for vb_condition_target, objects in grouped_cross_drawcalls.items():
                if not objects or not vb_condition_target:
                    continue

                section.append(f";跨 IB 身份块,绘制 {source_identifier} 需要跨 Ib 的物体引用")
                section.append(vb_condition_target)
                section.append(f"    cs-t2 = ResourceID_{source_identifier}")
                section.append(f"    run = CustomShader_RedirectCB1_{source_identifier}")
                section.append(f"    vs-t0 = ResourceFakeT0_SRV_{source_identifier}")
                section.append(f"    vs-cb2 = ResourceFakeCB1_{source_identifier}")
                section.append("    ;跨 IB 块数据区域")

                source_unique_str = source_submesh.unique_str
                section.append(f"    vb0 = Resource_{source_unique_str.replace('-', '_')}_Position")
                section.append(f"    vb1 = Resource_{source_unique_str.replace('-', '_')}_Texcoord")
                section.append(f"    vb2 = Resource_{source_unique_str.replace('-', '_')}_Blend")
                section.append(f"    vb3 = Resource_{source_unique_str.replace('-', '_')}_Position")
                src_ib_resource_name = "Resource_" + source_unique_str.replace('-', '_') + "_Index"
                section.append(f"    ib = {src_ib_resource_name}")

                section.append(";所有需要跨 Ib 的物体引用")

                self._append_drawindexed_instanced_with_shader_replace(
                    section,
                    objects,
                    getattr(source_drawib_model, "obj_name_draw_offset", None),
                )

                section.append("endif")

    def _copy_cross_ib_hlsl_files(self):
        addon_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
        source_dir = os.path.join(addon_dir, "Toolset")

        if not os.path.exists(source_dir):
            print(f"[CrossIB] 警告: Toolset目录不存在: {source_dir}")
            return

        hlsl_files = [
            'extract_cb1_ps.hlsl',
            'extract_cb1_vs.hlsl',
            'extract_capture_cb1_vs.hlsl',
            'record_bones_cs.hlsl',
            'redirect_cb1_cs.hlsl'
        ]

        refresh_hlsl_files = {
            'extract_cb1_vs.hlsl',
            'extract_capture_cb1_vs.hlsl',
        }

        mod_export_path = GlobalConfig.path_generate_mod_folder()
        res_dir = os.path.join(mod_export_path, "res")
        os.makedirs(res_dir, exist_ok=True)

        copied_count = 0
        for hlsl_file in hlsl_files:
            source_file = os.path.join(source_dir, hlsl_file)
            target_file = os.path.join(res_dir, hlsl_file)

            if os.path.exists(source_file):
                if hlsl_file in refresh_hlsl_files or not os.path.exists(target_file):
                    shutil.copy2(source_file, target_file)
                    print(f"[CrossIB] 已复制: {hlsl_file}")
                    copied_count += 1
                else:
                    print(f"[CrossIB] 文件已存在，跳过: {hlsl_file}")
            else:
                print(f"[CrossIB] 警告: 源文件不存在: {source_file}")

        print(f"[CrossIB] 共复制 {copied_count} 个HLSL文件到 {res_dir}")


    def _integrate_object_swap_ini_hook(self, ini_builder: M_IniBuilder):
        try:
            from ...blueprint.node_swap_ini import SwapKeyINIIntegrator
            from ...blueprint.export_helper import BlueprintExportHelper

            blueprint_tree = BlueprintExportHelper.get_current_blueprint_tree()
            if not blueprint_tree:
                return

            registry = getattr(self.blueprint_model, '_swap_key_registry', None)

            SwapKeyINIIntegrator.integrate_to_export(ini_builder, blueprint_tree, registry=registry)

        except ImportError:
            pass
        except Exception as e:
            from ...utils.log_utils import LOG
            LOG.warning(f"⚠️ 物体切换节点 INI 集成钩子执行失败: {e}")

    def export(self):
        try:
            # 合并骨架部件信息必须在缓冲区生成之前就绪（BlendRemap 导出依赖）。
            self.prepare_merged_skeleton()

            TimerUtils.start_stage("缓冲文件生成")
            self.generate_buffer_files()
            TimerUtils.end_stage("缓冲文件生成")

            TimerUtils.start_stage("INI配置生成")
            self.generate_ini_file()
            TimerUtils.end_stage("INI配置生成")
        finally:
            self._cleanup_stub_objects()

    def export_buffers_only(self):
        """只导出 Buffer 文件，不生成 INI 配置"""
        try:
            self.prepare_merged_skeleton()
            TimerUtils.start_stage("缓冲文件生成")
            self.generate_buffer_files()
            TimerUtils.end_stage("缓冲文件生成")
        finally:
            self._cleanup_stub_objects()
