"""拖拽交互后处理节点的纯逻辑测试（stub bpy，绕过 Blender 环境）。

加载策略：在 sys.modules 预置 `_ssmt_root` 包命名空间（blueprint/common/toolkit/utils，
仅 __path__，不执行任何真实 __init__.py），让 from . / from .. 相对导入按文件路径解析真实
模块；bpy 链上的 node_base / node_postprocess_base / toolkit.gb_core 用按需 stub 或文件级
加载，绕开会 import bpy 的 __init__ 链。

覆盖计划 v3.4 §9 的关键断言：
- 必需段齐全、二次执行幂等；
- 钩子注入位置（ib= 之后、第一个 run=/drawindexed= 之前）；ib=null 段不注入；
- ObjectMap 布局 (1+N)×16B、分区局部索引空间 (firstIndex=0, indexCount, mode=7, objectID=0)；
- Bake 偏移 = i×⌊part_count/8⌋、gs-t1 为 part IB 资源名；
- 稀疏区域 ABI：稳定 ID、Top-4 权重、ZoneParams 与 256 区域容量；
- ini 侧裁剪：无 dump 行、无 cs-u4/u7 绑定、有 cs-u2/u6/t73/t74、w84=0、x74=0、x75=0；
- boot-clear 块存在且幂等；grabbable 始终显式生成。
"""

import importlib.util
import os
import struct
import sys
import types
import unittest
from collections import OrderedDict
from pathlib import Path

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]


def _install_stub_bpy():
    """安装最小 bpy stub（满足节点模块的 bpy 引用）。

    discover 模式下其他测试可能先装了不兼容的 bpy stub（如 SimpleNamespace），
    这里检测兼容性，不兼容则替换。
    """
    existing = sys.modules.get("bpy")
    if existing is not None and isinstance(getattr(existing, "types", None), type) and hasattr(existing.types, "PropertyGroup"):
        return

    bpy = types.ModuleType("bpy")

    class _Props:
        def StringProperty(self, **kw): return None
        def IntProperty(self, **kw): return None
        def FloatProperty(self, **kw): return None
        def BoolProperty(self, **kw): return None
        def EnumProperty(self, **kw): return None
        def CollectionProperty(self, **kw): return None
        def PointerProperty(self, **kw): return None

    bpy.props = _Props()

    class _Types:
        class PropertyGroup: pass
        class Operator: pass
        class Object: pass
        class Collection: pass
        class Node: pass
        class NodeSocket: pass
        class Menu: pass
        class UIList: pass

    bpy.types = _Types()

    class _Utils:
        @staticmethod
        def register_class(cls): pass
        @staticmethod
        def unregister_class(cls): pass

    bpy.utils = _Utils()
    bpy.data = types.SimpleNamespace(objects=None, node_groups=None)
    bpy.context = types.SimpleNamespace(
        scene=None, active_object=None, selected_objects=())

    sys.modules["bpy"] = bpy
    sys.modules["bpy.types"] = bpy.types
    sys.modules["bpy.props"] = bpy.props


def _install_utils_error_stub(root):
    """utils.ssmt_error_utils（object_prefix_helper 依赖）。"""
    name = f"{root}.utils.ssmt_error_utils"
    if name in sys.modules:
        return
    mod = types.ModuleType(name)

    class SSMTErrorUtils:
        @staticmethod
        def log_and_raise(*a, **k):
            raise RuntimeError(a[0] if a else "SSMTError")

    mod.SSMTErrorUtils = SSMTErrorUtils
    sys.modules[name] = mod
    sys.modules[f"{root}.utils"].ssmt_error_utils = mod


def _install_node_base_stub(root):
    """node_postprocess_base 只依赖 node_base.SSMTNodeBase → 用 stub 满足。"""
    name = f"{root}.blueprint.node_base"
    if name in sys.modules:
        return
    nb = types.ModuleType(name)

    class SSMTNodeBase:
        pass

    nb.SSMTNodeBase = SSMTNodeBase
    sys.modules[name] = nb
    sys.modules[f"{root}.blueprint"].node_base = nb


def _install_toolkit_gb_core(root):
    """gb_core 纯 numpy（无 bpy），按文件加载挂到 toolkit stub 包下。"""
    name = f"{root}.toolkit.gb_core"
    if name in sys.modules:
        return
    path = REPO_ROOT / "toolkit" / "gb_core.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    sys.modules[f"{root}.toolkit"].gb_core = mod


def _load_drag_module():
    """用纯 stub 包路径加载节点模块（不执行任何真实 __init__.py）。"""
    _install_stub_bpy()
    # discover 模式下 test_bmtp_mesh_face_topology 等模块会把 sys.modules["numpy"]
    # 换成假实现（缺 float64 等属性），导致 export_space 等真实模块导入即崩；
    # 本模块顶层 np 在 import 阶段也可能已绑定假 numpy。这里从其他已加载模块的
    # 命名空间找回真实 numpy 模块对象（不能 pop+重导入：那会产生第二个 numpy
    # 实例，C 扩展子模块仍绑定旧实例，import numpy.linalg 会递归失败），
    # 并同步 sys.modules 与本模块全局 np 引用。
    global np
    current = sys.modules.get("numpy")
    if not hasattr(current, "float64") or not hasattr(np, "eye"):
        real_numpy = None
        for module in list(sys.modules.values()):
            for attr in ("np", "numpy"):
                try:
                    candidate = getattr(module, attr, None)
                except Exception:
                    continue
                if getattr(candidate, "__name__", None) == "numpy" and hasattr(candidate, "float64"):
                    real_numpy = candidate
                    break
            if real_numpy is not None:
                break
        if real_numpy is not None:
            sys.modules["numpy"] = real_numpy
            np = real_numpy
    import importlib

    root = "_ssmt_root"
    pkg_specs = {
        root: REPO_ROOT,
        f"{root}.blueprint": REPO_ROOT / "blueprint",
        f"{root}.common": REPO_ROOT / "common",
        f"{root}.toolkit": REPO_ROOT / "toolkit",
        f"{root}.utils": REPO_ROOT / "utils",
    }
    for name, path in pkg_specs.items():
        if name not in sys.modules:
            pkg = types.ModuleType(name)
            pkg.__path__ = [str(path)]
            sys.modules[name] = pkg

    _install_utils_error_stub(root)
    _install_node_base_stub(root)
    _install_toolkit_gb_core(root)

    return importlib.import_module(f"{root}.blueprint.node_postprocess_draginteraction")


def _make_node(mod, **props):
    """实例化节点类并注入属性默认值（绕过 bpy 属性系统）。"""
    node = mod.SSMTNode_PostProcess_DragInteraction.__new__(mod.SSMTNode_PostProcess_DragInteraction)
    defaults = dict(
        hash_values="", mod_namespace="", grab_key="ALT", grab_gesture="LMB", poke_gesture="RMB",
        enable_poke=True, enable_hand_cursor=False, enable_viewport_probe=True,
        drag_system_mode_default=2, drag_mode_initialized=True,
        drag_mode_variable_name="ssmtdrag_drag_enabled",
        mode_toggle_key="f8",
        shapekey_drive_move_sensitivity=0.02,
        ui_detected_variable_name="ssmtdrag_ui_detected",
        ui_zone_variable_name="ssmtdrag_ui_zone",
        phys_grab_damping=0.86, phys_grab_spring=0.176,
        phys_release_damping=0.96, phys_release_spring=0.055,
        phys_release_kick=0.12, phys_target_follow=1.10,
        mult_radius=1.0, mult_strength=0.333, mult_spring=0.333, mult_damping=1.0,
        zone_objects=[], bake_reference_object=None, mask_plateau=0.0,
        collision_enabled=False, collision_margin=0.002, collision_mode="SOFT",
        collision_point_budget=4096, collision_cell_size=0.0,
    )
    defaults.update(props)
    for k, v in defaults.items():
        object.__setattr__(node, k, v)
    return node


def _base_sections(hash_value="abc123", base_name="abc123-43191", vertex_count=14078):
    """构造一份标准 ZZMI ini sections（含 VLR、Position 资源、IB 绘制段）。"""
    return OrderedDict([
        ("[Constants]", ["global $active = 0"]),
        (f"[TextureOverride_{hash_value}_{base_name}_VertexLimitRaise]", [
            f"hash = {hash_value}",
            "override_byte_stride = 40",
            f"override_vertex_count = {vertex_count}",
            "uav_byte_stride = 4",
        ]),
        (f"[TextureOverride_{hash_value}_{base_name}A]", [
            f"hash = {hash_value}",
            "match_first_index = 0",
            f"ib = Resource{base_name}AIB",
            "run = CommandListSkinTexture",
            "drawindexed = 52688, 0, 0",
        ]),
        (f"[TextureOverride_{hash_value}_{base_name}B]", [
            f"hash = {hash_value}",
            "match_first_index = 52688",
            f"ib = Resource{base_name}BIB",
            # 生产形态：TheHerta4 的 drawindexed 偏移恒为 part 局部 0，
            # firstIndex 必须来自 match_first_index（回归：曾被 drawindexed 偏移覆盖成全 0）
            "drawindexed = 12000, 0, 0",
        ]),
        (f"[TextureOverride_{hash_value}_{base_name}Skip]", [
            f"hash = {hash_value}",
            "match_first_index = 0",
            "ib = null",
        ]),
        (f"[Resource_{hash_value}_Position]", [
            "type = Buffer",
            "stride = 40",
            f"filename = Meshes/{base_name}-Position.buf",
        ]),
        (f"[Resource{base_name}AIB]", ["type = Buffer", "format = DXGI_FORMAT_R32_UINT"]),
        (f"[Resource{base_name}BIB]", ["type = Buffer", "format = DXGI_FORMAT_R32_UINT"]),
        ("[Present]", []),
    ])


def _cross_ib_sections():
    return OrderedDict([
        ("[Constants]", []),
        ("[TextureOverride_sourcehash_sourcehash_VertexLimitRaise]", [
            "hash = sourcehash",
            "override_vertex_count = 1000",
        ]),
        ("[TextureOverride_targethash_targethash_VertexLimitRaise]", [
            "hash = targethash",
            "override_vertex_count = 2000",
        ]),
        ("[TextureOverride_targethash_0]", [
            "hash = targethash",
            "match_first_index = 0",
            "ib = Resource_targethash_0_Index",
            "; [mesh:LOD0.targethash-300-0.Target_copy] [vertex_count:200]",
            "drawindexed = 300, 0, 0",
            "ib = Resource_sourcehash_0_Index",
            "vb0 = ResourceBodyVB_sourcehash_0",
            "; [mesh:LOD0.sourcehash-120-0.SourceA_copy] [vertex_count:80]",
            "drawindexed = 90, 0, 0",
            "; [mesh:LOD0.sourcehash-120-0.SourceB_copy] [vertex_count:40]",
            "drawindexed = 30, 90, 0",
        ]),
        ("[Resource_sourcehash_Position]", [
            "type = Buffer", "stride = 40", "filename = Meshes/sourcehash-Position.buf",
        ]),
        ("[Resource_targethash_Position]", [
            "type = Buffer", "stride = 40", "filename = Meshes/targethash-Position.buf",
        ]),
        ("[Resource_sourcehash_0_Index]", ["type = Buffer", "format = DXGI_FORMAT_R32_UINT"]),
        ("[Resource_targethash_0_Index]", ["type = Buffer", "format = DXGI_FORMAT_R32_UINT"]),
        ("[Present]", []),
    ])


class DragNodeLocateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()

    def test_parse_hash_values_extracts_ib(self):
        node = _make_node(self.mod)
        self.assertEqual(node._parse_hash_values("abc123, def456"), ["abc123", "def456"])

    def test_collect_draw_parts_skips_ib_null(self):
        node = _make_node(self.mod)
        sections = _base_sections()
        parts = node._collect_draw_parts(sections, "abc123")
        # 跳过 ib=null 段，收集 A/B 两个 part
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0]["index_count"], 52688)
        self.assertEqual(parts[1]["first_index"], 52688)
        self.assertEqual(parts[0]["ib_resource"], "Resourceabc123-43191AIB")

    def test_collect_draw_parts_merges_copy_sections_with_same_ib(self):
        node = _make_node(self.mod)
        sections = _base_sections()
        sections["[TextureOverrideLOD0_abc123_43191A_copy]"] = [
            "hash = abc123",
            "match_first_index = 0",
            "ib = Resourceabc123-43191AIB",
            "; [mesh:LOD0.abc123-43191-0.CopyA] [vertex_count:80]",
            "drawindexed = 500, 52688, 0",
        ]

        parts = node._collect_draw_parts(sections, "abc123")
        part_a = next(p for p in parts if p["ib_resource"] == "Resourceabc123-43191AIB")

        self.assertEqual(len(parts), 2)
        self.assertEqual(part_a["index_count"], 53188)
        self.assertEqual(part_a["section"], "[TextureOverride_abc123_abc123-43191A]")

    def test_collect_draw_parts_combines_multiple_draws_in_one_ib_section(self):
        node = _make_node(self.mod)
        sections = _base_sections()
        section_name = "[TextureOverride_abc123_abc123-43191A]"
        sections[section_name][-1:] = [
            "drawindexed = 53994, 0, 0",
            "drawindexed = 3618, 53994, 0",
        ]

        part = node._collect_draw_parts(sections, "abc123")[0]

        self.assertEqual(part["ib_first_index"], 0)
        self.assertEqual(part["index_count"], 57612)

    def test_collect_draw_parts_uses_range_envelope_for_reused_and_unordered_draws(self):
        node = _make_node(self.mod)
        sections = _base_sections()
        section_name = "[TextureOverride_abc123_abc123-43191A]"
        sections[section_name][-1:] = [
            "DrawIndexed = 120, 300, 0",
            "drawindexed = 300, 0, 0",
            "DRAWINDEXED = 120, 300, 0",
        ]

        part = node._collect_draw_parts(sections, "abc123")[0]

        self.assertEqual(part["ib_first_index"], 0)
        self.assertEqual(part["index_count"], 420)

    def test_locate_component_preserves_shared_base_vertex(self):
        node = _make_node(self.mod)
        sections = _base_sections()
        for section_name in (
            "[TextureOverride_abc123_abc123-43191A]",
            "[TextureOverride_abc123_abc123-43191B]",
        ):
            sections[section_name][-1] = sections[section_name][-1].rsplit(",", 1)[0] + ", 3"

        comp = node._locate_components(sections, ["abc123"])[0]

        self.assertEqual(comp["vertex_base"], 3)
        self.assertTrue(all(part["vertex_base"] == 3 for part in comp["parts"]))

    def test_cross_ib_draws_are_owned_by_mesh_prefix_instead_of_section_hash(self):
        node = _make_node(self.mod)
        sections = _cross_ib_sections()

        source_parts = node._collect_draw_parts(sections, "sourcehash")
        target_parts = node._collect_draw_parts(sections, "targethash")

        self.assertEqual(len(source_parts), 1)
        self.assertEqual(source_parts[0]["ib_resource"], "Resource_sourcehash_0_Index")
        self.assertEqual(source_parts[0]["index_count"], 120)
        self.assertEqual(source_parts[0]["first_index"], 0)
        self.assertEqual(len(target_parts), 1)
        self.assertEqual(target_parts[0]["ib_resource"], "Resource_targethash_0_Index")
        self.assertEqual(target_parts[0]["index_count"], 300)

    def test_cross_ib_shader_replacement_draws_follow_mesh_prefix_and_run_section(self):
        node = _make_node(self.mod)
        sections = _cross_ib_sections()
        lines = sections["[TextureOverride_targethash_0]"]
        first_source_draw = lines.index("drawindexed = 90, 0, 0")
        second_source_draw = lines.index("drawindexed = 30, 90, 0")
        lines[first_source_draw] = "run = CustomShader_SourceA"
        lines[second_source_draw] = "run = CustomShader_SourceB"
        sections["[CustomShader_SourceA]"] = ["handling = skip", "drawindexed = 90, 0, 0"]
        sections["[CustomShader_SourceB]"] = ["handling = skip", "drawindexed = 30, 90, 0"]

        source_parts = node._collect_draw_parts(sections, "sourcehash")

        self.assertEqual(len(source_parts), 1)
        self.assertEqual(source_parts[0]["ib_resource"], "Resource_sourcehash_0_Index")
        self.assertEqual(source_parts[0]["index_count"], 120)
        self.assertEqual(
            source_parts[0]["hook_anchor_comment"],
            "; [mesh:LOD0.sourcehash-120-0.SourceA_copy] [vertex_count:80]",
        )

    def test_collect_draw_parts_captures_ttl_run_sites(self):
        node = _make_node(self.mod)
        sections = _base_sections(hash_value="deadbeef", base_name="deadbeef-43191")
        sections["[TextureOverrideLOD0_deadbeef_43191A_copy]"] = [
            "hash = deadbeef",
            "match_first_index = 0",
            "ib = Resourcedeadbeef-43191AIB",
            "; [mesh:LOD0.deadbeef-43191-0.CopyA] [vertex_count:80]",
            "if $swapkey6 == 5",
            "    $\\TTL\\_1 = 15255",
            "    $\\TTL\\_2 = 272901",
            "    $\\TTL\\_3 = 3",
            "    run = CommandListSSMTTTLDraw_deadbeef_A",
            "endif",
        ]

        parts = node._collect_draw_parts(sections, "deadbeef")
        part_a = next(p for p in parts if p["ib_resource"] == "Resourcedeadbeef-43191AIB")
        ttl_records = [r for r in part_a["draw_records"] if r["draw_offset"] == 272901]

        self.assertEqual(len(ttl_records), 1)
        self.assertEqual(ttl_records[0]["draw_count"], 15255)
        self.assertEqual(ttl_records[0]["vertex_base"], 3)

    def test_object_id_map_assigned_from_mesh_comments(self):
        node = _make_node(self.mod)
        sections = _cross_ib_sections()
        comps = node._locate_components(sections, ["targethash", "sourcehash"])

        # 两个组件的物体编号全局连续
        targethash = next(c for c in comps if c["hash"] == "targethash")
        sourcehash = next(c for c in comps if c["hash"] == "sourcehash")
        self.assertIn("LOD0.targethash-300-0.Target_copy", targethash["object_id_map"])
        self.assertIn("LOD0.sourcehash-120-0.SourceA_copy", sourcehash["object_id_map"])
        self.assertIn("LOD0.sourcehash-120-0.SourceB_copy", sourcehash["object_id_map"])
        all_ids = []
        for comp in comps:
            all_ids.extend(comp["object_id_map"].values())
        self.assertEqual(sorted(all_ids), list(range(len(all_ids))))

    def test_get_vertex_count_from_vlr(self):
        node = _make_node(self.mod)
        sections = _base_sections()
        self.assertEqual(node._get_vertex_count(sections, "abc123"), 14078)

    def test_read_position_buf_uses_custom_stride_and_position_offset(self):
        import tempfile

        node = _make_node(self.mod)
        node._get_vertex_struct_definition = lambda: (
            "struct VertexAttributes {\n"
            "    float padding;\n"
            "    float3 position;\n"
            "    float2 extra;\n"
            "};"
        )
        sections = OrderedDict({
            "[ResourceCustomPosition]": [
                "type = Buffer",
                "stride = 24",
                "filename = Meshes/custom-Position.buf",
            ],
        })
        comp = {"base_resource": "ResourceCustomPosition"}
        records = np.asarray([
            [99.0, 1.0, 2.0, 3.0, 10.0, 11.0],
            [98.0, 4.0, 5.0, 6.0, 12.0, 13.0],
        ], dtype=np.float32)

        with tempfile.TemporaryDirectory() as temp_dir:
            buffer_path = Path(temp_dir) / "Meshes" / "custom-Position.buf"
            buffer_path.parent.mkdir()
            records.tofile(buffer_path)
            positions = node._read_position_buf(temp_dir, sections, comp, vertex_count=2)

        np.testing.assert_array_equal(
            positions,
            np.asarray([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]], dtype=np.float32),
        )

    def test_read_component_triangles_labels_each_draw_range(self):
        import tempfile
        node = _make_node(self.mod)
        with tempfile.TemporaryDirectory() as temp_dir:
            ib_dir = Path(temp_dir) / "Meshes"
            ib_dir.mkdir()
            idx = np.array([0, 1, 2, 3, 4, 5], dtype=np.uint32)  # 两个三角形
            (ib_dir / "foo-Index.buf").write_bytes(idx.tobytes())

            sections = OrderedDict({
                "[Resourcefoo-Index]": [
                    "type = Buffer",
                    "filename = Meshes/foo-Index.buf",
                ],
            })
            comp = {
                "parts": [{
                    "ib_resource": "Resourcefoo-Index",
                    "name_ranges": [
                        (0, 3, "Body"),
                        (3, 3, "Head"),
                    ],
                }],
            }
            tri, names = node._read_component_triangles(temp_dir, sections, comp, 0)

        np.testing.assert_array_equal(tri, np.array([[0, 1, 2], [3, 4, 5]]))
        np.testing.assert_array_equal(names, np.array(["Body", "Head"], dtype=object))

    def test_read_removes_old_drag_tail_but_preserves_other_postprocess_modules(self):
        import tempfile

        node = _make_node(self.mod)
        content = "\n".join([
            "[Constants]",
            "global $active = 0",
            "[TextureOverride_Test]",
            "hash = abc123",
            "ib = ResourceTestIB",
            "\t; --- DRAG HOOK BEGIN ---",
            "\trun = CustomShaderDragBakeOld",
            "\t; --- DRAG HOOK END ---",
            "drawindexed = 3, 0, 0",
            self.mod.DRAG_TAIL_MARKER,
            "[ResourceOldDrag]",
            "type = Buffer",
            "; --- AUTO-APPENDED HEALTH DETECTION MODULE ---",
            "[ResourceHealth]",
            "type = Buffer",
            self.mod.DRAG_TAIL_MARKER,
            "[ResourceOldDragDuplicate]",
            "type = Buffer",
        ])

        with tempfile.TemporaryDirectory() as td:
            ini_path = Path(td) / "test.ini"
            ini_path.write_text(content, encoding="utf-8")
            sections, preserved_tail, _preserved_driver = node._read_ini_to_ordered_dict(str(ini_path))

        self.assertIn("[Constants]", sections)
        self.assertIn("drawindexed = 3, 0, 0", sections["[TextureOverride_Test]"])
        self.assertFalse(any("DRAG HOOK" in line for line in sections["[TextureOverride_Test]"]))
        self.assertFalse(any("CustomShaderDragBakeOld" in line for line in sections["[TextureOverride_Test]"]))
        self.assertNotIn("[ResourceOldDrag]", sections)
        self.assertNotIn(self.mod.DRAG_TAIL_MARKER, preserved_tail)
        self.assertNotIn("[ResourceOldDragDuplicate]", preserved_tail)
        self.assertIn("; --- AUTO-APPENDED HEALTH DETECTION MODULE ---", preserved_tail)
        self.assertIn("[ResourceHealth]", preserved_tail)


    def test_read_and_write_preserve_anim_driver_top_block(self):
        import tempfile

        node = _make_node(self.mod)
        driver_block = (
            "; --- ANIMATION DRIVER SECTION ---\n"
            "[Constants]\n"
            "global $driver_state = 0\n"
            "[Present]\n"
            "post $driver_state = 0\n"
            "; --- END ANIMATION DRIVER SECTION ---\n"
        )
        body = (
            "[Constants]\n"
            "global $body_state = 0\n"
            "[TextureOverride_Test]\n"
            "hash = abc123\n"
            "ib = ResourceTestIB\n"
            "drawindexed = 3, 0, 0\n"
        )

        with tempfile.TemporaryDirectory() as td:
            ini_path = Path(td) / "test.ini"
            ini_path.write_text(driver_block + "\n" + body, encoding="utf-8")
            sections, preserved_tail, preserved_driver = node._read_ini_to_ordered_dict(str(ini_path))
            node._write_ordered_dict_to_ini(sections, str(ini_path), preserved_tail, preserved_driver)
            written = ini_path.read_text(encoding="utf-8")

        self.assertIn("; --- ANIMATION DRIVER SECTION ---", written)
        self.assertIn("; --- END ANIMATION DRIVER SECTION ---", written)
        self.assertIn("global $driver_state = 0", written)
        self.assertIn("global $body_state = 0", written)
        self.assertNotIn("global $driver_state", sections["[Constants]"])

    def test_read_and_write_preserve_content_before_anim_driver_block(self):
        """动画驱动块即使不在首部，也不得吞掉它前面的正常 INI 段。"""
        import tempfile

        node = _make_node(self.mod)
        prefix = (
            "[Constants]\n"
            "global persist $body_state = 7\n\n"
            "[TextureOverride_BeforeDriver]\n"
            "hash = abc123\n"
            "drawindexed = 3, 0, 0\n\n"
        )
        driver_block = (
            "; --- ANIMATION DRIVER SECTION ---\n"
            "[Present]\n"
            "$driver_state = 1\n"
            "; --- END ANIMATION DRIVER SECTION ---\n\n"
        )
        suffix = (
            "[ResourceAfterDriver]\n"
            "type = Buffer\n"
            "format = R32_FLOAT\n"
        )

        with tempfile.TemporaryDirectory() as td:
            ini_path = Path(td) / "test.ini"
            ini_path.write_text(prefix + driver_block + suffix, encoding="utf-8")
            sections, preserved_tail, preserved_driver = node._read_ini_to_ordered_dict(str(ini_path))
            node._write_ordered_dict_to_ini(sections, str(ini_path), preserved_tail, preserved_driver)
            written = ini_path.read_text(encoding="utf-8")

        self.assertIn("global persist $body_state = 7", written)
        self.assertIn("[TextureOverride_BeforeDriver]", written)
        self.assertIn("[ResourceAfterDriver]", written)
        self.assertEqual(written.count("; --- ANIMATION DRIVER SECTION ---"), 1)

    def test_anim_driver_and_drag_ini_round_trip_is_idempotent(self):
        import tempfile

        node = _make_node(self.mod)
        original = (
            "[Constants]\n"
            "global persist $body_state = 7\n\n"
            "; --- ANIMATION DRIVER SECTION ---\n"
            "[Present]\n"
            "$driver_state = 1\n"
            "; --- END ANIMATION DRIVER SECTION ---\n\n"
            "[TextureOverride_Main]\n"
            "hash = abc123\n"
            "drawindexed = 3, 0, 0\n\n"
            "; --- AUTO-APPENDED HEALTH DETECTION MODULE ---\n"
            "[ResourceHealth]\n"
            "type = Buffer\n"
        )

        with tempfile.TemporaryDirectory() as td:
            ini_path = Path(td) / "test.ini"
            ini_path.write_text(original, encoding="utf-8")

            for iteration in range(2):
                sections, preserved_tail, preserved_driver = node._read_ini_to_ordered_dict(
                    str(ini_path)
                )
                node._write_ordered_dict_to_ini(
                    sections, str(ini_path), preserved_tail, preserved_driver
                )
                if iteration == 0:
                    first_write = ini_path.read_text(encoding="utf-8")

            second_write = ini_path.read_text(encoding="utf-8")

        self.assertEqual(second_write, first_write)
        self.assertEqual(second_write.count("; --- ANIMATION DRIVER SECTION ---"), 1)
        self.assertEqual(second_write.count("; --- AUTO-APPENDED HEALTH DETECTION MODULE ---"), 1)
        self.assertIn("global persist $body_state = 7", second_write)
        self.assertIn("[TextureOverride_Main]", second_write)

    def test_is_postprocess_node_on_export_chain_uses_pointer_identity(self):
        class FakeSocket:
            def __init__(self, links=None):
                self.links = links or []

        class FakeLink:
            def __init__(self, from_node, to_node):
                self.from_node = from_node
                self.to_node = to_node

        class FakeNode:
            def __init__(self, bl_idname, name, pointer):
                self.bl_idname = bl_idname
                self.name = name
                self.inputs = []
                self.outputs = []
                self._pointer = pointer
                self.id_data = types.SimpleNamespace(name="Main")

            def as_pointer(self):
                return self._pointer

        result = FakeNode("SSMTNode_Result_Output", "Result", 1)
        target_a = FakeNode("SSMTNode_PostProcess_DragInteraction", "Drag", 100)
        target_b = FakeNode("SSMTNode_PostProcess_DragInteraction", "Drag", 100)
        result.outputs = [FakeSocket([FakeLink(result, target_a)])]
        tree = types.SimpleNamespace(nodes=[result, target_a])

        self.assertTrue(self.mod.is_postprocess_node_on_export_chain(tree, target_b))


class DragNodeObjectMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()

    def test_object_map_layout_1_plus_n(self):
        node = _make_node(self.mod)
        sections = _base_sections()
        comp = node._locate_components(sections, ["abc123"])[0]
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            node._write_object_map(td, sections, comp)
            buf = Path(td) / "Meshes" / "abc123-43191ObjectMap.buf"
            data = buf.read_bytes()
        # (1+N)×16B = (1+2)×16 = 48B
        self.assertEqual(len(data), 48)
        header = struct.unpack('<ffff', data[:16])
        self.assertEqual(header[0], 2.0)  # part 数
        rec1 = struct.unpack('<ffff', data[16:32])
        self.assertEqual(rec1, (0.0, 52688.0, 7.0, 0.0))   # firstIndex, indexCount, mode=7, objectID
        rec2 = struct.unpack('<ffff', data[32:48])
        # 分区局部语义（对齐原作 ObjectMap 契约）：firstIndex 恒 0（detect 着色器
        # indexBase = firstIndex + tri*3，各部件 IB 为独立分区文件），objectID 恒 0
        #（着色器 entry.w==0 时回退 objectID=firstIndex=0，统一匹配碰撞档案条目 0）
        self.assertEqual(rec2, (0.0, 12000.0, 7.0, 0.0))

    def test_triangle_object_ids_bake(self):
        node = _make_node(self.mod)
        sections = _base_sections()
        comp = node._locate_components(sections, ["abc123"])[0]
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            # 伪造 part A 的 IB 文件（12 索引 = 4 三角形）
            ib_dir = Path(td) / "Meshes"
            ib_dir.mkdir(parents=True)
            ib = np.arange(12, dtype=np.uint32)
            ib.tofile(ib_dir / "abc123-43191AIB.buf")
            # 资源段声明 filename（指向伪造文件）
            sections["[Resourceabc123-43191AIB]"] = [
                "type = Buffer", "format = DXGI_FORMAT_R32_UINT",
                "filename = Meshes/abc123-43191AIB.buf",
            ]
            node._write_triangle_object_ids(td, sections, comp)

            out = ib_dir / "abc123-43191TriangleObjectIDsP0.buf"
            self.assertTrue(out.exists())
            ids = np.fromfile(out, dtype=np.uint32)
        # A 无 mesh 注释 → 无 name_ranges → 全部三角形保持未映射哨兵 0xFFFFFFFF
        self.assertEqual(len(ids), 4)
        self.assertTrue((ids == 0xFFFFFFFF).all())

    def test_triangle_object_ids_bake_maps_named_ranges(self):
        node = _make_node(self.mod)
        sections = _cross_ib_sections()
        comps = node._locate_components(sections, ["targethash"])
        comp = comps[0]
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ib_dir = Path(td) / "Meshes"
            ib_dir.mkdir(parents=True)
            ib = np.arange(300, dtype=np.uint32)
            ib.tofile(ib_dir / "targethash_0_Index.buf")
            sections["[Resource_targethash_0_Index]"] = [
                "type = Buffer", "format = DXGI_FORMAT_R32_UINT",
                "filename = Meshes/targethash_0_Index.buf",
            ]
            node._write_triangle_object_ids(td, sections, comp)
            ids = np.fromfile(ib_dir / "targethashTriangleObjectIDsP0.buf", dtype=np.uint32)
        target_id = comp["object_id_map"]["LOD0.targethash-300-0.Target_copy"]
        # 前 100 三角形 = Target_copy（draw 300@0）
        self.assertEqual(len(ids), 100)
        self.assertTrue((ids == target_id).all())

class DragNodeEmitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()

    def _emit(self, **props):
        node = _make_node(self.mod, **props)
        sections = _base_sections()
        comps = node._locate_components(sections, ["abc123"])
        node._emit_sections(sections, comps, "testns")
        return node, sections, comps

    @staticmethod
    def _zone_item(zone_id, weight=1.0, enabled=True, grabbable=True):
        settings = types.SimpleNamespace(
            enabled=enabled,
            brush_strength=weight,
            brush_falloff_k=4.6,
            radius=0.0,
            strength=0.0,
            max_offset=0.0,
            falloff=0.0,
            damping=0.0,
            grabbable=grabbable,
        )
        empty = types.SimpleNamespace(
            name=f"zone_{zone_id}",
            ssmt_drag_zone=settings,
            matrix_world=np.eye(4),
        )
        return types.SimpleNamespace(zone_id=zone_id, zone_object=empty)

    def test_required_sections_present(self):
        _, sections, _ = self._emit()
        cn = "abc123_43191"
        required = [
            f"[CustomShaderDragDetect{cn}_testns]",
            f"[CustomShaderDragJiggle{cn}_testns]",
            f"[CustomShaderDragPinDetected_testns]",
            f"[CustomShaderDragUpdateScreenJiggle_testns]",
            f"[CustomShaderDragPinComponent{cn}_testns]",
            f"[CommandListDragPinDetected_testns]",
            f"[CommandListDragCursorUpdate_testns]",
            f"[ResourceDragJiggleTempVB0_{cn}_testns]",
            "[KeyDragInputManagerLMB_testns]",
            "[KeyDragInputManagerModifier_testns]",
        ]
        for sec in required:
            self.assertIn(sec, sections, f"缺少必需段 {sec}")

    def test_present_without_shapekey_drive_does_not_reference_seed_flag(self):
        node, sections, components = self._emit(enable_shapekey_drive=False)

        node._emit_present_and_constants(sections, components, "testns")

        self.assertFalse(
            any("ssmtdrag_seed_pending_testns" in line for line in sections["[Present]"])
        )

    def test_empty_namespace_uses_stable_default_A(self):
        node = _make_node(self.mod, mod_namespace="")

        self.assertEqual(node._resolve_namespace("Character.ini"), "A")
        self.assertEqual(node._resolve_namespace(""), "A")

        custom = _make_node(self.mod, mod_namespace="My Mod-01")
        self.assertEqual(custom._resolve_namespace("Character.ini"), "My_Mod_01")

    def test_click_export_entries_reject_invalid_zone_and_bound_cycle_length(self):
        invalid = types.SimpleNamespace(
            bl_idname="SSMTNode_AnimDriver_ClickExport",
            click_zone_id=256,
            cycle_length=999,
            click_target_list=[types.SimpleNamespace(variable_name="$Invalid")],
        )
        valid = types.SimpleNamespace(
            bl_idname="SSMTNode_AnimDriver_ClickExport",
            click_zone_id=2,
            cycle_length=999,
            click_target_list=[types.SimpleNamespace(variable_name="$Valid")],
        )
        anim_tree = types.SimpleNamespace(name="AnimTree", nodes=[invalid, valid])
        postprocess = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_AnimDriver",
            blueprint_name="AnimTree",
        )
        node = _make_node(self.mod, enable_shapekey_drive=True)
        node.id_data = types.SimpleNamespace(nodes=[postprocess])

        class _Groups(list):
            def get(self, name, default=None):
                return next((item for item in self if item.name == name), default)

        self.mod.bpy.data.node_groups = _Groups([anim_tree])

        self.assertEqual(node._collect_click_export_drivers(), [(2, 64, "$Valid")])

    def test_click_export_entries_ignore_muted_animation_driver_nodes(self):
        muted = types.SimpleNamespace(
            bl_idname="SSMTNode_AnimDriver_ClickExport",
            mute=True,
            click_zone_id=4,
            cycle_length=5,
            click_target_list=[types.SimpleNamespace(variable_name="$Muted")],
        )
        enabled = types.SimpleNamespace(
            bl_idname="SSMTNode_AnimDriver_ClickExport",
            mute=False,
            click_zone_id=2,
            cycle_length=3,
            click_target_list=[types.SimpleNamespace(variable_name="$Enabled")],
        )
        anim_tree = types.SimpleNamespace(name="AnimTree", nodes=[muted, enabled])
        postprocess = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_AnimDriver",
            blueprint_name="AnimTree",
        )
        node = _make_node(self.mod, enable_shapekey_drive=True)
        node.id_data = types.SimpleNamespace(nodes=[postprocess])

        class _Groups(list):
            def get(self, name, default=None):
                return next((item for item in self if item.name == name), default)

        self.mod.bpy.data.node_groups = _Groups([anim_tree])

        self.assertEqual(node._collect_click_export_drivers(), [(2, 3, "$Enabled")])

    def test_click_export_entries_ignore_muted_anim_driver_postprocess_node(self):
        click_node = types.SimpleNamespace(
            bl_idname="SSMTNode_AnimDriver_ClickExport",
            mute=False,
            click_zone_id=2,
            cycle_length=3,
            click_target_list=[types.SimpleNamespace(variable_name="$Enabled")],
        )
        anim_tree = types.SimpleNamespace(name="AnimTree", nodes=[click_node])
        postprocess = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_AnimDriver",
            mute=True,
            blueprint_name="AnimTree",
        )
        node = _make_node(self.mod, enable_shapekey_drive=True)
        node.id_data = types.SimpleNamespace(nodes=[postprocess])

        class _Groups(list):
            def get(self, name, default=None):
                return next((item for item in self if item.name == name), default)

        self.mod.bpy.data.node_groups = _Groups([anim_tree])

        self.assertEqual(node._collect_click_export_drivers(), [])

    def test_click_export_entries_ignore_disconnected_anim_driver_postprocess_node(self):
        connected_click = types.SimpleNamespace(
            bl_idname="SSMTNode_AnimDriver_ClickExport",
            mute=False,
            click_zone_id=2,
            cycle_length=3,
            click_target_list=[types.SimpleNamespace(variable_name="$Connected")],
        )
        disconnected_click = types.SimpleNamespace(
            bl_idname="SSMTNode_AnimDriver_ClickExport",
            mute=False,
            click_zone_id=9,
            cycle_length=10,
            click_target_list=[types.SimpleNamespace(variable_name="$Disconnected")],
        )
        connected_tree = types.SimpleNamespace(name="ConnectedAnim", nodes=[connected_click])
        disconnected_tree = types.SimpleNamespace(name="DisconnectedAnim", nodes=[disconnected_click])

        result = types.SimpleNamespace(
            bl_idname="SSMTNode_Result_Output", inputs=[], outputs=[]
        )
        connected = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_AnimDriver",
            mute=False,
            blueprint_name="ConnectedAnim",
            inputs=[],
            outputs=[],
        )
        disconnected = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_AnimDriver",
            mute=False,
            blueprint_name="DisconnectedAnim",
            inputs=[],
            outputs=[],
        )
        link = types.SimpleNamespace(from_node=result, to_node=connected)
        result.outputs.append(types.SimpleNamespace(links=[link]))
        connected.inputs.append(types.SimpleNamespace(links=[link]))

        node = _make_node(self.mod, enable_shapekey_drive=True)
        node.id_data = types.SimpleNamespace(nodes=[result, connected, disconnected])

        class _Groups(list):
            def get(self, name, default=None):
                return next((item for item in self if item.name == name), default)

        self.mod.bpy.data.node_groups = _Groups([connected_tree, disconnected_tree])

        self.assertEqual(node._collect_click_export_drivers(), [(2, 3, "$Connected")])

    def test_click_export_expands_sparse_buffer_and_emits_seed_bindings(self):
        click_node = types.SimpleNamespace(
            bl_idname="SSMTNode_AnimDriver_ClickExport",
            click_zone_id=3,
            cycle_length=4,
            click_target_list=[types.SimpleNamespace(variable_name="$Swap")],
        )
        anim_tree = types.SimpleNamespace(name="AnimTree", nodes=[click_node])
        postprocess = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_AnimDriver",
            blueprint_name="AnimTree",
        )
        node = _make_node(self.mod, enable_shapekey_drive=True)
        node.id_data = types.SimpleNamespace(nodes=[postprocess])

        class _Groups(list):
            def get(self, name, default=None):
                return next((item for item in self if item.name == name), default)

        self.mod.bpy.data.node_groups = _Groups([anim_tree])

        self.assertEqual(node._click_export_seed_entries(), [(3, "$Swap")])

        total, bases, counts = node._drag_drive_buffer_layout()
        self.assertEqual(counts, [1, 1, 1, 3])
        self.assertEqual(bases, [0, 5, 10, 15])
        self.assertEqual(total, 22)

        sections = _base_sections()
        node._emit_sections(sections, [], "testns")
        node._emit_present_and_constants(sections, [], "testns")

        self.assertEqual(
            sections["[ResourceDragShapeKeyDrive_testns]"],
            ["type = RWBuffer", "format = R32_FLOAT", "array = 22"],
        )
        shader = sections["[CustomShaderDragShapeKeyDrive_testns]"]
        self.assertIn("x81 = 1", shader)
        self.assertIn("x82 = 3", shader)
        self.assertIn("y82 = $Swap", shader)
        pin = sections["[CommandListDragPinDetected_testns]"]
        self.assertIn("\t$ssmtdrag_seed_pending_testns = 1", pin)
        self.assertLess(
            pin.index("\trun = CustomShaderDragShapeKeyDrive_testns"),
            pin.index("\t$ssmtdrag_seed_pending_testns = 0"),
        )
        present = sections["[Present]"]
        boot_run = "if $ssmtdrag_booted_testns == 0"
        seed_run = "elif $ssmtdrag_seed_pending_testns == 1"
        interaction_run = "elif $ssmtdrag_drag_enabled_testns >= 1"
        self.assertIn(boot_run, present)
        self.assertIn(seed_run, present)
        self.assertLess(present.index(boot_run), present.index(seed_run))
        self.assertLess(present.index(seed_run), present.index(interaction_run))

    def test_click_export_seed_entries_reject_more_than_eight_unique_zones(self):
        click_nodes = [
            types.SimpleNamespace(
                bl_idname="SSMTNode_AnimDriver_ClickExport",
                click_zone_id=zone,
                cycle_length=2,
                click_target_list=[types.SimpleNamespace(variable_name=f"$Swap{zone}")],
            )
            for zone in range(10)
        ]
        click_nodes.insert(
            1,
            types.SimpleNamespace(
                bl_idname="SSMTNode_AnimDriver_ClickExport",
                click_zone_id=0,
                cycle_length=2,
                click_target_list=[types.SimpleNamespace(variable_name="$Duplicate")],
            ),
        )
        anim_tree = types.SimpleNamespace(name="AnimTree", nodes=click_nodes)
        postprocess = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_AnimDriver",
            blueprint_name="AnimTree",
        )
        node = _make_node(self.mod, enable_shapekey_drive=True)
        node.id_data = types.SimpleNamespace(nodes=[postprocess])

        class _Groups(list):
            def get(self, name, default=None):
                return next((item for item in self if item.name == name), default)

        self.mod.bpy.data.node_groups = _Groups([anim_tree])

        with self.assertRaisesRegex(ValueError, "最多支持 8 个不同区域"):
            node._click_export_seed_entries()

    def test_shapekey_drive_dir_resource_and_bindings(self):
        zone = self._zone_item(0)
        node, sections, comps = self._emit(
            enable_shapekey_drive=True,
            zone_objects=[zone],
        )
        node._emit_present_and_constants(sections, comps, "testns")

        self.assertEqual(
            sections["[ResourceDragShapeKeyDrive_testns]"],
            ["type = RWBuffer", "format = R32_FLOAT", "array = 5"],
        )
        # 方向缓冲 = 区域×档位×5槽（4方向 + 1无方向）+ 1（末位槽存上一帧按键状态）；
        # 拖拽绑定锁存独立为单槽资源（编码 0=未绑定，否则 区域id+1）
        self.assertEqual(
            sections["[ResourceDragShapeKeyDir_testns]"],
            ["type = RWBuffer", "format = R32_FLOAT", "array = 6"],
        )
        self.assertEqual(
            sections["[ResourceDragShapeKeyDragLatch_testns]"],
            ["type = RWBuffer", "format = R32_FLOAT", "array = 1"],
        )
        self.assertEqual(
            sections["[ResourceDragShapeKeyClickCount_testns]"],
            ["type = RWBuffer", "format = R32_UINT", "array = 1"],
        )
        self.assertEqual(
            sections["[ResourceDragShapeKeyClickCountF_testns]"],
            ["type = RWBuffer", "format = R32_FLOAT", "array = 1"],
        )
        self.assertEqual(
            sections["[ResourceDragShapeKeyActiveDir_testns]"],
            ["type = RWBuffer", "format = R32_UINT", "array = 1"],
        )

        cs = sections["[CustomShaderDragShapeKeyDrive_testns]"]
        self.assertIn("cs-u0 = ResourceDragShapeKeyDrive_testns", cs)
        self.assertIn("cs-u1 = ResourceDragShapeKeyDir_testns", cs)
        self.assertIn("cs-u2 = ResourceDragShapeKeyClickCount_testns", cs)
        self.assertIn("cs-u3 = ResourceDragShapeKeyActiveDir_testns", cs)
        self.assertIn("cs-u4 = ResourceDragShapeKeyClickCountF_testns", cs)
        # 绑定锁存资源绑定给驱动 CS（u5）
        self.assertIn("cs-u5 = ResourceDragShapeKeyDragLatch_testns", cs)
        self.assertIn("post cs-u5 = null", cs)
        self.assertIn("post cs-u1 = null", cs)
        self.assertIn("z77 = $ssmtdrag_drag_enabled_testns", cs)
        self.assertIn("w77 = $ssmtdrag_lmb_down_testns", cs)
        self.assertIn("x78 = $ssmtdrag_x_down_testns", cs)

        pin = sections["[CommandListDragPinDetected_testns]"]
        self.assertIn("\tclear = ResourceDragShapeKeyDrive_testns 0.0", pin)
        self.assertIn("\tclear = ResourceDragShapeKeyDir_testns 0.0", pin)
        # boot 清零绑定锁存（编码 0=未绑定，不得假绑 zone 0）；
        # 失臂 else 分支同样整清锁存（松 Alt/undraw/模式 0 时驱动 CS 不运行）
        self.assertIn("\tclear = ResourceDragShapeKeyDragLatch_testns 0.0", pin)
        disarm_idx = next(
            i for i, line in enumerate(pin)
            if line == "\t$ObjectDetectAllowed_testns = 0")
        self.assertIn("clear = ResourceDragShapeKeyDragLatch_testns", pin[disarm_idx + 1])

        present = sections["[Present]"]
        gate_idx = next(
            i for i, line in enumerate(present) if "--- DRAG INTERACTION GATE BEGIN ---" in line
        )
        gate_block = present[gate_idx:gate_idx + 80]
        # 形态键缓冲只在 boot 清零；模式切换不再清零，保证任意模式都保持已调整的数值
        self.assertNotIn("if $ssmtdrag_drag_enabled_testns != 1", gate_block)
        self.assertNotIn("clear = ResourceDragShapeKeyDrive_testns 0.0", gate_block)
        self.assertNotIn("clear = ResourceDragShapeKeyClickCount_testns", gate_block)
        # 评审 G1：模式 0（含 1→0 直跳，无 dispatch）由 Present 终态 else 清锁存，
        # 防陈旧绑定跨 mode-0 滞留、回模式 1 后无命中复活
        elif_idx = next(
            i for i, line in enumerate(present)
            if line == "elif $ssmtdrag_drag_enabled_testns >= 1")
        tail = present[elif_idx:]
        else_idx = next(i for i, line in enumerate(tail) if line == "else")
        self.assertEqual(tail[else_idx + 1], "\tclear = ResourceDragShapeKeyDragLatch_testns 0.0")
        self.assertEqual(tail[else_idx + 2], "endif")

    def test_shapekey_drive_mouse_displacement_present_lines(self):
        zone = self._zone_item(0)
        node, sections, comps = self._emit(
            enable_shapekey_drive=True,
            zone_objects=[zone],
            shapekey_drive_move_sensitivity=0.02,
        )
        node._emit_present_and_constants(sections, comps, "testns")

        present = "\n".join(sections["[Present]"])
        self.assertIn("$ssmtdrag_shapekey_dy_testns = $cursorY - $ssmtdrag_shapekey_prev_y_testns", present)
        self.assertIn("$ssmtdrag_shapekey_dx_testns = $cursorX - $ssmtdrag_shapekey_prev_x_testns", present)

        constants = "\n".join(sections["[Constants]"])
        self.assertIn("global $ssmtdrag_shapekey_dy_testns = 0", constants)
        self.assertIn("global $ssmtdrag_shapekey_dx_testns = 0", constants)
        self.assertIn("global $ssmtdrag_shapekey_prev_y_testns = 0", constants)
        self.assertIn("global $ssmtdrag_shapekey_prev_x_testns = 0", constants)

        cs = sections["[CustomShaderDragShapeKeyDrive_testns]"]
        self.assertIn("x79 = $ssmtdrag_shapekey_dy_testns", cs)
        self.assertIn("y79 = $ssmtdrag_shapekey_dx_testns", cs)
        self.assertNotIn("w79", cs)
        self.assertNotIn("x77", cs)
        self.assertNotIn("y77", cs)
        self.assertIn("x80 = 0.02", cs)

    def test_shapekey_drive_zone_stage_counts_auto_derived_from_shapekey_nodes(self):
        zone = self._zone_item(0)
        node = _make_node(
            self.mod,
            enable_shapekey_drive=True,
            zone_objects=[zone],
        )
        # 同树形态键节点：A 区域0 档位 2、B 区域0 档位 1、C 方向形态键（不计入档位）、
        # D 未开启拖拽驱动（档位 3 不应计入）
        sk_a = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_ShapeKey",
            drag_drive_enabled=True,
            shapekey_variable_items=[
                types.SimpleNamespace(shape_key_name="A", drag_zone_id=0, drag_dir_id="-1", drag_click_stage=2),
            ],
        )
        sk_b = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_ShapeKey",
            drag_drive_enabled=True,
            shapekey_variable_items=[
                types.SimpleNamespace(shape_key_name="B", drag_zone_id=0, drag_dir_id="-1", drag_click_stage=1),
            ],
        )
        sk_dir = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_ShapeKey",
            drag_drive_enabled=True,
            shapekey_variable_items=[
                types.SimpleNamespace(shape_key_name="C", drag_zone_id=0, drag_dir_id="0", drag_click_stage=3),
            ],
        )
        sk_off = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_ShapeKey",
            drag_drive_enabled=False,
            shapekey_variable_items=[
                types.SimpleNamespace(shape_key_name="D", drag_zone_id=0, drag_dir_id="-1", drag_click_stage=3),
            ],
        )
        node.id_data = types.SimpleNamespace(nodes=[sk_a, sk_b, sk_dir, sk_off])
        # 区域 0 无方向档位最大 2；方向形态键档位 3 不计入
        self.assertEqual(node._drag_drive_zone_stage_counts(), {0: 2})
        total, bases, counts = node._drag_drive_buffer_layout()
        self.assertEqual(total, 6)   # 4 方向 + 2 无方向档位
        self.assertEqual(bases, [0])
        self.assertEqual(counts, [2])

        sections = _base_sections()
        comps = node._locate_components(sections, ["abc123"])
        node._emit_sections(sections, comps, "testns")
        node._emit_present_and_constants(sections, comps, "testns")

        self.assertEqual(
            sections["[ResourceDragShapeKeyDrive_testns]"],
            ["type = RWBuffer", "format = R32_FLOAT", "array = 6"],
        )
        self.assertEqual(
            sections["[ResourceDragShapeKeyDir_testns]"],
            ["type = RWBuffer", "format = R32_FLOAT", "array = 7"],
        )
        self.assertEqual(
            sections["[ResourceDragShapeKeyDragLatch_testns]"],
            ["type = RWBuffer", "format = R32_FLOAT", "array = 1"],
        )
        cs = sections["[CustomShaderDragShapeKeyDrive_testns]"]
        self.assertIn("cs-t68 = ResourceDragShapeKeyZoneStageCounts_testns", cs)
        self.assertNotIn("z79", cs)

    @staticmethod
    def _fake_sk_node(items, enabled=True):
        return types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_ShapeKey",
            drag_drive_enabled=enabled,
            shapekey_variable_items=[
                types.SimpleNamespace(
                    shape_key_name=name,
                    drag_zone_id=zone,
                    drag_dir_id=dir_id,
                    drag_click_stage=stage,
                )
                for name, zone, dir_id, stage in items
            ],
            get_shape_key_export_variable_name=lambda name: f"$Freq_{name}",
        )

    def test_shapekey_var_sync_bindings_slot_layout(self):
        zone = self._zone_item(0)
        node = _make_node(
            self.mod,
            enable_shapekey_drive=True,
            zone_objects=[zone],
        )
        # A 无方向档位 2 → 槽 4+(2-1)=5；B 方向 0 → 槽 0；C 未绑定区域跳过；D 节点未开启跳过
        node.id_data = types.SimpleNamespace(nodes=[
            self._fake_sk_node([("A", 0, "-1", 2)]),
            self._fake_sk_node([("B", 0, "0", 3)]),
            self._fake_sk_node([("C", -1, "-1", 1)]),
            self._fake_sk_node([("D", 0, "-1", 1)], enabled=False),
        ])
        bindings = node._drag_drive_var_sync_bindings()
        self.assertEqual(bindings, [
            ("$Freq_A", 5, 0, 2),
            ("$Freq_B", 0, 0, -1),
        ])

    def test_drag_drive_skips_unchecked_shapekey_items(self):
        """未勾选导出（export_enabled=False）的形态键不参与档位统计与变量同步绑定：
        其变量不会生成到 INI，拖拽侧也不得引用。"""
        zone = self._zone_item(0)
        node = _make_node(
            self.mod,
            enable_shapekey_drive=True,
            zone_objects=[zone],
        )
        checked = types.SimpleNamespace(
            shape_key_name="A", drag_zone_id=0, drag_dir_id="-1", drag_click_stage=1,
            export_enabled=True,
        )
        unchecked = types.SimpleNamespace(
            shape_key_name="B", drag_zone_id=0, drag_dir_id="-1", drag_click_stage=3,
            export_enabled=False,
        )
        sk_node = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_ShapeKey",
            drag_drive_enabled=True,
            shapekey_variable_items=[checked, unchecked],
            get_shape_key_export_variable_name=lambda name: f"$Freq_{name}",
        )
        node.id_data = types.SimpleNamespace(nodes=[sk_node])

        # 未勾选的 B（档位 3）不计入 → 区域 0 档位为已勾选 A 的 1
        self.assertEqual(node._drag_drive_zone_stage_counts(), {0: 1})
        # 未勾选的 B 不产生同步绑定
        bindings = node._drag_drive_var_sync_bindings()
        self.assertEqual(bindings, [("$Freq_A", 4, 0, 1)])

    def test_shapekey_var_sync_sections_and_present_run(self):
        zone = self._zone_item(0)
        node = _make_node(
            self.mod,
            enable_shapekey_drive=True,
            zone_objects=[zone],
        )
        node.id_data = types.SimpleNamespace(nodes=[
            self._fake_sk_node([("A", 0, "-1", 2), ("B", 0, "0", 1)]),
        ])
        sections = _base_sections()
        comps = node._locate_components(sections, ["abc123"])
        node._emit_sections(sections, comps, "testns")
        node._emit_present_and_constants(sections, comps, "testns")

        self.assertEqual(
            sections["[ResourceDragShapeKeyVarPrev_testns]"],
            ["type = RWBuffer", "format = R32_FLOAT", "array = 2"],
        )
        self.assertEqual(
            sections["[ResourceDragShapeKeyVarSyncMap_testns]"],
            ["type = Buffer", "format = R32G32B32A32_UINT",
             "filename = res/drag_interaction/ShapeKeyVarSyncMap_testns.buf"],
        )
        # 拖拽激活标志（每区域）：同步 CS 每帧镜像驱动 CS 的绑定锁存，CPU store 直接读取
        self.assertEqual(
            sections["[ResourceDragShapeKeyZoneActive_testns]"],
            ["type = RWBuffer", "format = R32_FLOAT", "array = 1"],
        )
        # 无镜像/克隆等中间层（store 对任意 D3D11 缓冲成立，直接读源缓冲）
        self.assertNotIn("[ResourceDragShapeKeyVarReadback_testns]", sections)
        self.assertNotIn("[ResourceDragShapeKeyVarRBCopy_testns]", sections)
        self.assertNotIn("[ResourceDragShapeKeyZoneActiveCopy_testns]", sections)

        cs = sections["[CustomShaderDragShapeKeyVarSync_testns]"]
        self.assertIn("cs = res/drag_interaction/rzm_shapekey_var_sync.hlsl", cs)
        # 门控输入固定 IniParams[75]（模式/按住/绘制/输入模式）
        self.assertIn("x75 = $ssmtdrag_drag_enabled_testns", cs)
        self.assertIn("y75 = $ssmtdrag_skheld_testns", cs)
        self.assertIn("z75 = $ssmtdrag_drawn_testns", cs)
        self.assertIn("w75 = $inputMode", cs)
        # 变量 4 个一组打包进 IniParams[81+]
        self.assertIn("x81 = $Freq_A", cs)
        self.assertIn("y81 = $Freq_B", cs)
        self.assertNotIn("$ssmtdrag_skrb", "\n".join(cs))
        self.assertIn("cs-t67 = ResourceDragPinnedDetectInfo_testns", cs)
        self.assertIn("cs-t69 = ResourceDragShapeKeyVarSyncMap_testns", cs)
        self.assertIn("cs-u0 = ResourceDragShapeKeyDrive_testns", cs)
        self.assertIn("cs-u1 = ResourceDragShapeKeyClickCount_testns", cs)
        self.assertIn("cs-u2 = ResourceDragShapeKeyVarPrev_testns", cs)
        self.assertIn("cs-u3 = ResourceDragShapeKeyClickCountF_testns", cs)
        self.assertIn("cs-u4 = ResourceDragShapeKeyZoneActive_testns", cs)
        # 绑定锁存（独立单槽资源）是 ZoneActive 的单一事实源
        self.assertIn("cs-u5 = ResourceDragShapeKeyDragLatch_testns", cs)
        self.assertIn("dispatch = 1, 1, 1", cs)
        self.assertIn("post cs-t67 = null", cs)
        self.assertIn("post cs-u4 = null", cs)
        self.assertIn("post cs-u5 = null", cs)

        pin = sections["[CommandListDragPinDetected_testns]"]
        self.assertIn("\tclear = ResourceDragShapeKeyVarPrev_testns 0.0", pin)
        self.assertIn("\tclear = ResourceDragShapeKeyZoneActive_testns 0.0", pin)
        self.assertNotIn("ResourceDragShapeKeyVarReadback_testns", "\n".join(pin))
        self.assertIn("$ssmtdrag_skpending_testns_0 = 0", "\n".join(pin))
        self.assertIn("$ssmtdrag_skmode_testns_0 = 0", "\n".join(pin))

        present = "\n".join(sections["[Present]"])
        self.assertIn("run = CustomShaderDragShapeKeyVarSync_testns", present)
        self.assertIn("$ssmtdrag_skheld_testns = 0", present)
        self.assertIn(
            "if $ssmtdrag_mode_testns == 1 && ($ssmtdrag_lmb_down_testns == 1 || $ssmtdrag_x_down_testns == 1)",
            present,
        )
        # store 回读放在命名命令列表内、pre run 调用（对齐血量库 hp.ini 形态）
        self.assertIn("pre run = CommandListDragShapeKeyVarReadback_testns", present)

        rb = "\n".join(sections["[CommandListDragShapeKeyVarReadback_testns]"])
        # store 直接读源缓冲（无镜像/克隆）
        self.assertNotIn(" = copy ", rb)
        # 变量优先握手：变化后 pending=1/mode=2，持续回推直到回读追平；
        # 只有追平后才允许拖拽/缓冲变化以 mode=1 拉回变量。
        self.assertIn("store = $ssmtdrag_skact_testns_0, ResourceDragShapeKeyZoneActive_testns, 0", rb)
        self.assertIn("store = $ssmtdrag_skrb_testns_0, ResourceDragShapeKeyDrive_testns, 5", rb)
        self.assertIn("if $Freq_A != $ssmtdrag_skprev_testns_0", rb)
        self.assertIn("$ssmtdrag_skprev_testns_0 = $Freq_A", rb)
        self.assertIn("$ssmtdrag_skpending_testns_0 = 1", rb)
        self.assertIn("$ssmtdrag_skmode_testns_0 = 2", rb)
        self.assertIn("elif $ssmtdrag_skpending_testns_0 == 1", rb)
        self.assertIn("if $ssmtdrag_skrb_testns_0 == $ssmtdrag_skprev_testns_0", rb)
        self.assertIn("elif $ssmtdrag_skact_testns_0 >= 1", rb)
        self.assertIn("$Freq_A = $ssmtdrag_skrb_testns_0", rb)
        self.assertIn("$ssmtdrag_skmode_testns_0 = 1", rb)
        self.assertIn("store = $ssmtdrag_skrb_testns_1, ResourceDragShapeKeyDrive_testns, 0", rb)
        self.assertIn("$Freq_B = $ssmtdrag_skrb_testns_1", rb)

        constants = "\n".join(sections["[Constants]"])
        self.assertIn("global $ssmtdrag_skheld_testns = 0", constants)
        self.assertIn("global $ssmtdrag_skact_testns_0 = 0", constants)
        self.assertIn("global $ssmtdrag_skrb_testns_0 = 0", constants)
        self.assertIn("global $ssmtdrag_skprev_testns_0 = 0", constants)
        self.assertIn("global $ssmtdrag_skpending_testns_0 = 0", constants)
        self.assertIn("global $ssmtdrag_skmode_testns_0 = 0", constants)
        self.assertIn("global $ssmtdrag_skrb_testns_1 = 0", constants)

        # 同步模式区从 90 开始，与最多 36 个变量使用的 81..89 完全分离。
        cs = "\n".join(sections["[CustomShaderDragShapeKeyVarSync_testns]"])
        self.assertIn("x90 = $ssmtdrag_skmode_testns_0", cs)
        self.assertIn("y90 = $ssmtdrag_skmode_testns_1", cs)

    def test_shapekey_var_sync_nine_bindings_do_not_overlap_iniparams(self):
        zone = self._zone_item(0)
        node = _make_node(
            self.mod,
            enable_shapekey_drive=True,
            zone_objects=[zone],
        )
        node.id_data = types.SimpleNamespace(nodes=[
            self._fake_sk_node([(f"SK{i}", 0, "0", 1) for i in range(9)]),
        ])
        sections = {}
        node._emit_shapekey_var_sync_section(sections, "testns")
        lines = sections["[CustomShaderDragShapeKeyVarSync_testns]"]
        self.assertIn("x83 = $Freq_SK8", lines)
        self.assertIn("x90 = $ssmtdrag_skmode_testns_0", lines)
        param_targets = [
            line.split("=", 1)[0].strip()
            for line in lines
            if line and line[0] in "xyzw" and "=" in line
        ]
        self.assertEqual(len(param_targets), len(set(param_targets)))

    def test_shapekey_var_sync_skipped_without_bindings(self):
        zone = self._zone_item(0)
        node = _make_node(
            self.mod,
            enable_shapekey_drive=True,
            zone_objects=[zone],
        )
        # 同树没有任何形态键节点 → 无绑定，不生成同步资源/段/运行行
        node.id_data = types.SimpleNamespace(nodes=[])
        sections = _base_sections()
        comps = node._locate_components(sections, ["abc123"])
        node._emit_sections(sections, comps, "testns")
        node._emit_present_and_constants(sections, comps, "testns")

        self.assertNotIn("[ResourceDragShapeKeyVarPrev_testns]", sections)
        self.assertNotIn("[ResourceDragShapeKeyVarSyncMap_testns]", sections)
        self.assertNotIn("[ResourceDragShapeKeyVarReadback_testns]", sections)
        self.assertNotIn("[ResourceDragShapeKeyZoneActive_testns]", sections)
        self.assertNotIn("[ResourceDragShapeKeyVarRBCopy_testns]", sections)
        self.assertNotIn("[ResourceDragShapeKeyZoneActiveCopy_testns]", sections)
        self.assertNotIn("[CustomShaderDragShapeKeyVarSync_testns]", sections)
        self.assertNotIn("[CommandListDragShapeKeyVarReadback_testns]", sections)
        pin = sections["[CommandListDragPinDetected_testns]"]
        self.assertNotIn("ResourceDragShapeKeyVarPrev_testns", "\n".join(pin))
        self.assertNotIn("$ssmtdrag_skcd_testns", "\n".join(pin))
        present = "\n".join(sections["[Present]"])
        self.assertNotIn("CustomShaderDragShapeKeyVarSync_testns", present)
        self.assertNotIn("$ssmtdrag_skrb_testns", present)
        constants = "\n".join(sections["[Constants]"])
        self.assertNotIn("$ssmtdrag_skrb_testns", constants)
        self.assertNotIn("$ssmtdrag_skact_testns", constants)
        self.assertNotIn("$ssmtdrag_skheld_testns", constants)

    def test_shapekey_var_sync_map_baked_to_buf(self):
        import tempfile
        zone = self._zone_item(0)
        node = _make_node(
            self.mod,
            enable_shapekey_drive=True,
            zone_objects=[zone],
        )
        node.id_data = types.SimpleNamespace(nodes=[
            self._fake_sk_node([("A", 0, "-1", 2), ("B", 0, "0", 1)]),
        ])
        out_dir = tempfile.mkdtemp(prefix="drag_var_sync_test_")
        try:
            node._write_zone_resources(out_dir, "testns")
            buf_path = os.path.join(
                out_dir, "res", "drag_interaction", "ShapeKeyVarSyncMap_testns.buf")
            self.assertTrue(os.path.isfile(buf_path))
            data = np.fromfile(buf_path, dtype=np.uint32)
            # A：槽 5、区域 0、档位 2；B：槽 0、区域 0、方向键档位哨兵 0xFFFFFFFF
            self.assertEqual(
                data.tolist(),
                [5, 0, 2, 0, 0, 0, 0xFFFFFFFF, 0],
            )
        finally:
            import shutil
            shutil.rmtree(out_dir, ignore_errors=True)

    def test_shapekey_var_sync_shader_structure(self):
        shader_path = os.path.join("Toolset", "drag_interaction", "rzm_shapekey_var_sync.hlsl")
        if not os.path.exists(shader_path):
            self.skipTest("shader missing")
        with open(shader_path, encoding="utf-8") as f:
            content = f.read()
        # 与驱动 CS 共享 ShapeKeyDrive / ClickCount，自带 prev 值缓冲做变更检测，
        # 激活标志为 RWBuffer（u4）；无镜像缓冲（store 直接读源缓冲）
        self.assertIn("RWBuffer<float> ShapeKeyDrive       : register(u0);", content)
        self.assertIn("RWBuffer<uint>  ClickCount          : register(u1);", content)
        self.assertIn("RWBuffer<float> VarSyncPrev         : register(u2);", content)
        self.assertIn("RWBuffer<float> ClickCountF         : register(u3);", content)
        self.assertIn("RWBuffer<float> ZoneActive          : register(u4);", content)
        self.assertIn("Buffer<uint4>   VarSyncMap          : register(t69);", content)
        self.assertIn("StructuredBuffer<float4> PinnedDetectInfo : register(t67);", content)
        self.assertNotIn("RWBuffer<float> VarReadback", content)
        # 变量 4 个一组打包：从 IniParams[81] 起（76-80 为驱动 CS 占用）
        self.assertIn("#define VAR_SYNC_INIPARAM_BASE 81", content)
        # 命中判定门控已移除：ZoneActive 改由驱动 CS 的绑定锁存镜像（单一事实源）
        self.assertNotIn("#define VAR_SYNC_GATE_PARAMS 75", content)
        self.assertIn("#define VAR_SYNC_MODE_BASE 90", content)
        self.assertIn("[90 + i/4][i%4] = CPU/GPU arbitration mode", content)
        self.assertNotIn("[83 + i/4][i%4] = CPU readback pull flag", content)
        self.assertIn("IniParams[VAR_SYNC_INIPARAM_BASE + (i >> 2)][i & 3]", content)
        # 激活标志镜像驱动 CS 的绑定锁存资源（DragLatch[0]，编码 0=未绑定/区域id+1）：
        # 按住命中即绑定、移出区域不丢、松开沿/失臂解除
        self.assertIn("RWBuffer<float> DragLatch           : register(u5);", content)
        self.assertIn("DragLatch[0]", content)
        # 评审 G2：头部契约注释不得残留旧资源名（锁存已迁独立 DragLatch 资源）
        self.assertNotIn("ShapeKeyDir latch slot", content)
        self.assertIn("ResourceDragShapeKeyDragLatch slot", content)
        self.assertIn("ZoneActive[z] = (boundZone >= 0 && z == (uint)boundZone) ? 1.0 : 0.0;", content)
        # 仅在变量真实变化时处理（不变则不覆写拖拽结果）
        self.assertIn("if (abs(raw - VarSyncPrev[i]) <= 1e-6)", content)
        self.assertIn("VarSyncPrev[i] = raw;", content)
        # 变量为主：变化即回写（不再挂起拖拽激活帧）；仅 CPU 回读回声帧跳过
        self.assertNotIn("if (zone < clickSlots && ZoneActive[zone] > 0.5)", content)
        self.assertIn("IniParams[VAR_SYNC_MODE_BASE + (i >> 2)][i & 3]", content)
        self.assertIn("syncMode > 1.5", content)
        self.assertIn("ShapeKeyDrive[slot] = v;", content)
        self.assertIn("ClickCountF[zone] = (float)ClickCount[zone];", content)
        # 无方向档位：变量非 0 打开对应档位，归 0 时仅清空本档位
        self.assertIn("ClickCount[zone] = ndStage;", content)
        self.assertIn("ClickCount[zone] = 0u;", content)

    def test_shapekey_drive_shader_stage_cycle_and_no_direction_set(self):
        shader_path = os.path.join("Toolset", "drag_interaction", "rzm_shapekey_drive.hlsl")
        if not os.path.exists(shader_path):
            self.skipTest("shader missing")
        with open(shader_path, encoding="utf-8") as f:
            content = f.read()
        # 点击档位按区域独立（ZoneStageCounts）在 0..N 间循环：0=清空，第 N+1 次点击回到 0
        self.assertIn("uint zoneStageCount = max(1u, ZoneStageCounts[hoverZone]);", content)
        self.assertIn("uint newStage = oldStage >= zoneStageCount ? 0u : oldStage + 1u;", content)
        self.assertNotIn("(oldStage % stageCount) + 1u", content)
        # 无方向槽：命中该档位时置 1，非活动档位归 0（不是翻转）
        self.assertIn("if (activeStage == stage && zonePressed)", content)
        self.assertIn("ShapeKeyDrive[ndIdx] = 1.0;", content)
        self.assertIn("ShapeKeyDrive[ndIdx] = 0.0;", content)
        self.assertNotIn("cur > 0.5 ? 0.0 : 1.0", content)
        # 每区域独立段：4 方向槽 + N 无方向槽，运行基址前缀和
        self.assertIn("runningBase += 4u + zoneStageCount;", content)
        self.assertIn("Buffer<uint> ZoneStageCounts        : register(t68);", content)

    def test_shapekey_drive_shader_zones_are_independent(self):
        shader_path = os.path.join("Toolset", "drag_interaction", "rzm_shapekey_drive.hlsl")
        if not os.path.exists(shader_path):
            self.skipTest("shader missing")
        with open(shader_path, encoding="utf-8") as f:
            content = f.read()
        # 各区域独立段基址 + 独立档位数
        self.assertIn("uint zoneBase = runningBase;", content)
        self.assertIn("uint zoneStageCount = max(1u, ZoneStageCounts[zone]);", content)
        # 无方向槽置位必须同时满足“本区域命中按下”（zoneHit && pressed），
        # 防止在其他区域按下时误置本区域槽
        self.assertIn("bool zoneHit = hasHit && zone == hoverZone;", content)
        self.assertIn("bool zonePressed = zoneHit && pressed;", content)
        self.assertIn("if (activeStage == stage && zonePressed)", content)
        # 方向槽位移积分由锁存绑定（zoneDriven）门控：绑定后光标移出区域不丢控，
        # 未绑定区域保持不积分；当帧实时命中（zoneHit）不再参与位移门控
        self.assertIn("bool zoneDriven = boundZone >= 0 && zone == (uint)boundZone;", content)
        self.assertIn("if (zoneDriven)", content)
        self.assertNotIn("if (zoneHit)\n", content)

    def test_mode_toggle_key_generates_cycle_section(self):
        _, sections, _ = self._emit()
        key_lines = sections["[KeyDragInputManagerModeToggle_testns]"]
        self.assertEqual(key_lines, [
            "key = f8",
            "type = cycle",
            "$ssmtdrag_drag_enabled_testns = 0,1,2",
        ])

    def test_mode_toggle_key_custom_mode_variable(self):
        _, sections, _ = self._emit(drag_mode_variable_name="$custom_drag_mode")
        key_lines = sections["[KeyDragInputManagerModeToggle_testns]"]
        self.assertIn("$custom_drag_mode = 0,1,2", key_lines)

    def test_mode_toggle_key_update_and_always_generate(self):
        node = _make_node(self.mod, mode_toggle_key="f9")
        sections = _base_sections()
        comps = node._locate_components(sections, ["abc123"])
        node._emit_sections(sections, comps, "testns")
        self.assertEqual(sections["[KeyDragInputManagerModeToggle_testns]"][0], "key = f9")

        # 再次导出保持幂等（不重复、不追加）
        node._emit_sections(sections, comps, "testns")
        self.assertEqual(len(sections["[KeyDragInputManagerModeToggle_testns]"]), 3)

        # 清空时也始终生成（键名直接用字段内容）
        node2 = _make_node(self.mod, mode_toggle_key="")
        node2._emit_sections(sections, comps, "testns")
        key_lines = sections["[KeyDragInputManagerModeToggle_testns]"]
        self.assertEqual(key_lines, [
            "key = ",
            "type = cycle",
            "$ssmtdrag_drag_enabled_testns = 0,1,2",
        ])

    def test_jiggle_temp_vb0_empty_declaration_and_copy_roundtrip(self):
        # R9 回退：恢复 TempVB0 空声明 + copy 往返自愈链（Alt 模型消失回归修复）
        _, sections, _ = self._emit()
        self.assertEqual(
            sections["[ResourceDragJiggleTempVB0_abc123_43191_testns]"],
            ["type = RWBuffer"],
        )
        self.assertNotIn("[ResourceDragJiggleShadowVB_abc123_43191_testns]", sections)
        jig = "\n".join(sections["[CustomShaderDragJiggleabc123_43191_testns]"])
        # 自愈链：copy-in → u5=copy → vb0=null → copy-out（CS 不写也有完整网格）
        self.assertIn("ResourceDragJiggleTempVB0_abc123_43191_testns = vb0", jig)
        self.assertIn("cs-t24 = vb0", jig)
        self.assertIn("cs-u5 = copy ResourceDragJiggleTempVB0_abc123_43191_testns", jig)
        self.assertIn("vb0 = null", jig)
        self.assertIn("ResourceDragJiggleTempVB0_abc123_43191_testns = copy cs-u5", jig)
        self.assertNotIn("ShadowVB", jig)
        self.assertIn("Dispatch = 55, 1, 1", jig)  # ceil(14078/256)

    def test_legacy_jiggle_vb0_sections_stripped_on_read(self):
        # 迁移：历代命名（TempVB0 空声明段 / ShadowVB 真声明段）在重读时都被剔除，
        # 由发射侧按当前形态（TempVB0）重建，避免双份并存
        node = _make_node(self.mod)
        sections = _base_sections()
        sections["[ResourceDragJiggleTempVB0_abc123_43191_testns]"] = ["type = RWBuffer"]
        sections["[ResourceDragJiggleShadowVB_abc123_43191_testns]"] = [
            "type = RWStructuredBuffer", "stride = 40", "array = 14078",
        ]
        node._remove_existing_legacy_jiggle_vb0_sections(sections)
        self.assertNotIn("[ResourceDragJiggleTempVB0_abc123_43191_testns]", sections)
        self.assertNotIn("[ResourceDragJiggleShadowVB_abc123_43191_testns]", sections)

    def test_jiggle_reemit_overwrites_shadowvb_form(self):
        # 迁移：R9 ShadowVB 时代段落在重导出时整体替换为 TempVB0 copy 形态
        node = _make_node(self.mod)
        sections = _base_sections()
        sections["[CustomShaderDragJiggleabc123_43191_testns]"] = [
            "cs-t24 = vb0",
            "cs-u5 = ResourceDragJiggleShadowVB_abc123_43191_testns",
            "Dispatch = 55, 1, 1",
        ]
        sections["[ResourceDragJiggleShadowVB_abc123_43191_testns]"] = [
            "type = RWStructuredBuffer", "stride = 40", "array = 14078",
        ]
        node._remove_existing_legacy_jiggle_vb0_sections(sections)
        comps = node._locate_components(sections, ["abc123"])
        node._emit_sections(sections, comps, "testns")
        jig = "\n".join(sections["[CustomShaderDragJiggleabc123_43191_testns]"])
        self.assertIn("cs-u5 = copy ResourceDragJiggleTempVB0_abc123_43191_testns", jig)
        self.assertIn("ResourceDragJiggleTempVB0_abc123_43191_testns = copy cs-u5", jig)
        self.assertNotIn("ShadowVB", jig)
        self.assertNotIn("[ResourceDragJiggleShadowVB_abc123_43191_testns]", sections)

    def test_hook_rebind_targets_temp_vb0_every_part(self):
        # 换绑一致性：全部件钩子块 vb0 换绑目标必须为 TempVB0（灰模防线）
        node = _make_node(self.mod)
        sections = _base_sections()
        comps = node._locate_components(sections, ["abc123"])
        node._emit_sections(sections, comps, "testns")
        for comp in comps:
            node._inject_draw_hooks(sections, comp, "testns")
        rebinds = [
            line.strip()
            for lines in sections.values()
            for line in lines
            if line.strip().startswith("vb0 = ResourceDragJiggle")
        ]
        self.assertTrue(rebinds, "应存在 jiggle 换绑行")
        for line in rebinds:
            self.assertIn("ResourceDragJiggleTempVB0_abc123_43191_testns", line)
            self.assertNotIn("ShadowVB", line)

    def test_ttl_copy_sections_get_slim_hook(self):
        # 回归（2026-09-02 实机：TTL 层完全不吃形态键/拖拽，与本体一层分歧）：
        # 引用 CommandListSSMTTTLDraw 的材质 copy 段必须获得段级钩子——命令
        # 列表内层的 vb0 换绑传播不到 run = 嵌套命令列表的绘制，仅 CL 内换绑时
        # TTLib 离屏捕获读到的是段级 RedirectSO 原始数据。
        # 钩子为瘦身形态（无 Bake/Detect）：逐段重复 Bake/Detect dispatch 是
        # 86e3c86 去重要消的开销；jiggle 烘焙 time 守卫每帧至多一次。
        node = _make_node(self.mod)
        sections = _base_sections()
        comps = node._locate_components(sections, ["abc123"])
        node._emit_sections(sections, comps, "testns")
        copy_sec = "[TextureOverrideLOD0_abc123_abc123-43191A_deadbeef_copy]"
        sections[copy_sec] = [
            "hash = abc123",
            "match_first_index = 0",
            "vb0 = ResourceZZRedirectSO_deadbeef",
            "ib = Resourceabc123-43191AIB",
            "run = CommandListSkinTexture",
            "if $swapkey0 == 1",
            "    run = CommandListSSMTTTLDraw_abc123_43191_testns",
            "endif",
        ]
        for comp in comps:
            node._inject_draw_hooks(sections, comp, "testns")
        copy_lines = sections[copy_sec]
        copy_text = "\n".join(copy_lines)
        self.assertIn("DRAG HOOK BEGIN abc123_43191P0_testns", copy_text)
        self.assertIn("vb0 = ResourceDragJiggleTempVB0_abc123_43191_testns", copy_text)
        self.assertNotIn("CustomShaderDragBake", copy_text)
        self.assertNotIn("CustomShaderDragDetect", copy_text)
        # 钩子落点：SkinTexture 之后、TTL 条件块之前（与旧版可用布局一致）
        skin_idx = next(i for i, l in enumerate(copy_lines)
                        if l.strip() == "run = CommandListSkinTexture")
        hook_idx = next(i for i, l in enumerate(copy_lines)
                        if "DRAG HOOK BEGIN" in l)
        cond_idx = next(i for i, l in enumerate(copy_lines)
                        if l.strip() == "if $swapkey0 == 1")
        self.assertLess(skin_idx, hook_idx)
        self.assertLess(hook_idx, cond_idx)

    def test_ttl_copy_hook_reexport_idempotent(self):
        # 重导出链路：读入剥离（_remove_existing_draw_hooks）后必须重建 copy
        # 段钩子，且重复注入不产生重复钩子（回归：重导出后 TTL 段钩子永久丢失）
        node = _make_node(self.mod)
        sections = _base_sections()
        comps = node._locate_components(sections, ["abc123"])
        node._emit_sections(sections, comps, "testns")
        copy_sec = "[TextureOverrideLOD0_abc123_abc123-43191A_deadbeef_copy]"
        sections[copy_sec] = [
            "hash = abc123",
            "match_first_index = 0",
            "ib = Resourceabc123-43191AIB",
            "run = CommandListSkinTexture",
            "    run = CommandListSSMTTTLDraw_abc123_43191_testns",
        ]
        for comp in comps:
            node._inject_draw_hooks(sections, comp, "testns")
        # 模拟重导出：读入剥离 → 重新注入
        node._remove_existing_draw_hooks(sections)
        self.assertNotIn("DRAG HOOK BEGIN", "\n".join(sections[copy_sec]))
        comps = node._locate_components(sections, ["abc123"])
        for comp in comps:
            node._inject_draw_hooks(sections, comp, "testns")
        text = "\n".join(sections[copy_sec])
        self.assertEqual(text.count("DRAG HOOK BEGIN"), 1)

    def test_bake_offsets_part_local(self):
        _, sections, _ = self._emit()
        # part A: index_count=52688 → step = 52688//8 = 6586；R5 段合并后单 sample 段
        #（8P → P），偏移经 [Constants] global 迭代变量推导
        s1 = sections["[CustomShaderDragBakeSample_abc123_43191P0_testns]"]
        self.assertIn("local $ssmtdrag_bake_off_abc123_43191P0_testns", s1)
        self.assertIn(
            "$ssmtdrag_bake_off_abc123_43191P0_testns = "
            "$ssmtdrag_bake_base_abc123_43191P0_testns + "
            "$ssmtdrag_bake_i_abc123_43191P0_testns * "
            "$ssmtdrag_bake_step_abc123_43191P0_testns",
            s1,
        )
        self.assertIn("x26 = $ssmtdrag_bake_i_abc123_43191P0_testns", s1)
        self.assertIn("y26 = $ssmtdrag_bake_off_abc123_43191P0_testns", s1)
        self.assertIn("drawindexed = 1, $ssmtdrag_bake_off_abc123_43191P0_testns, 0", s1)
        self.assertIn("gs-t1 = Resourceabc123-43191AIB", s1)
        # part B 用自己的 IB 资源名
        s1b = sections["[CustomShaderDragBakeSample_abc123_43191P1_testns]"]
        self.assertIn("gs-t1 = Resourceabc123-43191BIB", s1b)
        # [Constants] 必须发射三个 global 迭代变量（t6 §4.3 作用域修复）
        constants = "\n".join(sections["[Constants]"])
        self.assertIn("global $ssmtdrag_bake_base_abc123_43191P0_testns = 0", constants)
        self.assertIn("global $ssmtdrag_bake_step_abc123_43191P0_testns = 6586", constants)
        self.assertIn("global $ssmtdrag_bake_i_abc123_43191P0_testns = 0", constants)
        # 旧 8 段全部消失，仅 1 个合并 sample 段；bake 段 = 1 unbind + 1 clear +
        # 1 初始化 + 8 run + 7 自增
        for i in range(8):
            self.assertNotIn(f"[CustomShaderDragBakeSample{i}_abc123_43191P0_testns]", sections)
        self.assertIn("[CustomShaderDragBakeSample_abc123_43191P0_testns]", sections)
        bake = sections["[CustomShaderDragBakeabc123_43191P0_testns]"]
        self.assertEqual(
            [line for line in bake if line.startswith("run = CustomShaderDragBakeSample")],
            ["run = CustomShaderDragBakeSample_abc123_43191P0_testns"] * 8,
        )

    def test_redirect_base_vertex_reaches_bake_and_detect(self):
        node = _make_node(self.mod)
        sections = _base_sections()
        for section_name in (
            "[TextureOverride_abc123_abc123-43191A]",
            "[TextureOverride_abc123_abc123-43191B]",
        ):
            sections[section_name][-1] = sections[section_name][-1].rsplit(",", 1)[0] + ", 3"
        comps = node._locate_components(sections, ["abc123"])

        node._emit_sections(sections, comps, "testns")

        sample = sections["[CustomShaderDragBakeSample_abc123_43191P0_testns]"]
        self.assertIn("drawindexed = 1, $ssmtdrag_bake_off_abc123_43191P0_testns, 3", sample)
        detect = sections["[CustomShaderDragDetectabc123_43191P0_testns]"]
        self.assertIn("z26 = 3", detect)

    def test_bake_offsets_cover_multiple_draws_in_one_ib_section(self):
        node = _make_node(self.mod)
        sections = _base_sections()
        section_name = "[TextureOverride_abc123_abc123-43191A]"
        sections[section_name][-1:] = [
            "drawindexed = 53994, 0, 0",
            "drawindexed = 3618, 53994, 0",
        ]
        comps = node._locate_components(sections, ["abc123"])

        node._emit_sections(sections, comps, "testns")

        # 双 draw 合并后 index_count=57612 → step=7201；R5 后 base/step 落在 [Constants]
        constants = "\n".join(sections["[Constants]"])
        self.assertIn("global $ssmtdrag_bake_base_abc123_43191P0_testns = 0", constants)
        self.assertIn("global $ssmtdrag_bake_step_abc123_43191P0_testns = 7201", constants)
        sample = sections["[CustomShaderDragBakeSample_abc123_43191P0_testns]"]
        self.assertIn(
            "$ssmtdrag_bake_off_abc123_43191P0_testns = "
            "$ssmtdrag_bake_base_abc123_43191P0_testns + "
            "$ssmtdrag_bake_i_abc123_43191P0_testns * "
            "$ssmtdrag_bake_step_abc123_43191P0_testns",
            sample,
        )

    def test_sparse_zone_resources_replace_fixed_register_block(self):
        _, sections, _ = self._emit()
        jig = "\n".join(sections["[CustomShaderDragJiggleabc123_43191_testns]"])
        self.assertIn("cs-t65 = ResourceDragJiggleZoneIDs", jig)
        self.assertIn("cs-t66 = ResourceDragJiggleZoneWeights", jig)
        self.assertIn("cs-t75 = ResourceDragZoneParams", jig)
        self.assertIn("x119 = 0", jig)
        self.assertNotIn("x77 = ", jig)
        self.assertNotIn("x99 = ", jig)

    def test_x72_asymmetry_mirrors_original(self):
        # 显式传 mult_radius 以与默认值解耦：Jiggle y72 来自 mult_radius 属性，
        # UpdateScreenJiggle y72 恒为硬编码 1.0（不对称，照原作）
        _, sections, _ = self._emit(mult_radius=0.333)
        jig = "\n".join(sections["[CustomShaderDragJiggleabc123_43191_testns]"])
        usj = "\n".join(sections["[CustomShaderDragUpdateScreenJiggle_testns]"])
        self.assertIn("y72 = 0.333", jig)   # Jiggle y72 = mult_radius
        self.assertIn("y72 = 1.0", usj)      # UpdateScreenJiggle y72 = 1.0（不对称，照原作）

    def test_ini_side_trims(self):
        _, sections, _ = self._emit()
        jig = "\n".join(sections["[CustomShaderDragJiggleabc123_43191_testns]"])
        det = "\n".join(sections["[CustomShaderDragDetectabc123_43191_testns]"])
        usj = "\n".join(sections["[CustomShaderDragUpdateScreenJiggle_testns]"])
        # 无 dump 行、无 cs-u4/u7 绑定
        for text in (jig, det, usj):
            self.assertNotIn("dump =", text)
            self.assertNotIn("cs-u4", text)
            self.assertNotIn("cs-u7", text)
        # 有 cs-u2/u6/t73/t74
        self.assertIn("cs-u6 = ResourceDragJiggleState", jig)
        self.assertIn("cs-t73 = ResourceDragPathVectors", jig)
        self.assertIn("cs-t74 = ResourceDragPathProgressState", jig)
        self.assertIn("cs-u2 = ResourceDragDebugDetect", det)
        # w84=0、x74=0、x75=0
        self.assertIn("w84 = 0", usj)
        self.assertIn("x74 = 0", jig)
        self.assertIn("x75 = 0", jig)
        # VIEWPORT_VALID 必须为 1（回归：x86=0 会让检测主循环被 ValidViewportCursor 永远拒绝）
        self.assertIn("x86 = 1", det)
        self.assertNotIn("x86 = 0", det)

    def test_per_part_detect_sections_limit_object_range(self):
        # 性能回归：旧行为在每个部件钩子里 raycast 全部件三角形范围（N 倍放大）。
        # 每部件变体段以 y28/z28 限定只测本部件条目，基座段保持旧默认（遍历全部件）
        _, sections, _ = self._emit()
        cn = "abc123_43191"
        base = "\n".join(sections[f"[CustomShaderDragDetect{cn}_testns]"])
        self.assertNotIn("y28 =", base)
        self.assertNotIn("z28 =", base)
        for p_idx in range(2):
            part = "\n".join(sections[f"[CustomShaderDragDetect{cn}P{p_idx}_testns]"])
            self.assertIn(f"y28 = {p_idx}", part)
            self.assertIn("z28 = 1", part)
            # 其余绑定与基座一致（含 x86=1 主循环门控）
            self.assertIn("x86 = 1", part)
            self.assertIn(f"cs-t2 = ResourceDragDetect{cn}ObjectMap_testns", part)

    def test_hook_runs_bake_detect_and_uses_per_part_section(self):
        # 每帧每个钩子仍正常跑 Bake+Detect，不得用帧内去重标志吞掉后续 pass；
        # 且 Detect 只跑本部件的变体段
        node, sections, comps = self._emit()
        comp = comps[0]
        node._inject_draw_hooks(sections, comp, "testns")
        cn = "abc123_43191"
        for p_idx, section in enumerate((
            "[TextureOverride_abc123_abc123-43191A]",
            "[TextureOverride_abc123_abc123-43191B]",
        )):
            text = "\n".join(sections[section])
            self.assertIn(f"run = CustomShaderDragBake{cn}P{p_idx}_testns", text)
            self.assertIn(f"run = CustomShaderDragDetect{cn}P{p_idx}_testns", text)
            self.assertNotIn(f"run = CustomShaderDragDetect{cn}_testns", text)
            self.assertNotIn("detect_run", text)

    def test_constants_do_not_declare_per_part_detect_run(self):
        node, sections, comps = self._emit()
        node._emit_present_and_constants(sections, comps, "testns")
        const = "\n".join(sections["[Constants]"])
        self.assertNotIn("detect_run", const)

    def test_detect_shader_supports_object_subrange(self):
        # 着色器契约：IniParams[28].yz 限定 ObjectMap 子区间，z28=0 回退遍历全部件
        shader = (Path(__file__).resolve().parents[1]
                  / "Toolset" / "drag_interaction" / "rzm_object_detect.hlsl"
                  ).read_text(encoding="utf-8")
        self.assertIn("#define DETECT_OBJECT_FIRST IniParams[28].y", shader)
        self.assertIn("#define DETECT_OBJECT_SPAN  IniParams[28].z", shader)
        self.assertIn("#define DETECT_VERTEX_BASE", shader)
        self.assertIn("gIndexBuffer[indexBase + 0u] + DETECT_VERTEX_BASE", shader)
        self.assertIn("uint vertexIndex = (uint)(meta.x + 0.5f);", shader)
        self.assertIn("vertexIndex += DETECT_VERTEX_BASE;", shader)
        self.assertIn("localPos = gVB0[vertexIndex].position;", shader)
        self.assertIn(
            "for (uint objectIndex = objectFirst; objectIndex < objectLast; objectIndex++)",
            shader)
        self.assertIn(": objectCount;", shader)

    def test_sparse_mask_resource_sections(self):
        _, sections, _ = self._emit()
        cn = "abc123_43191"
        ids = sections[f"[ResourceDragJiggleZoneIDs_{cn}_testns]"]
        weights = sections[f"[ResourceDragJiggleZoneWeights_{cn}_testns]"]
        self.assertIn("format = R32G32B32A32_UINT", ids)
        self.assertIn("format = R32G32B32A32_FLOAT", weights)
        self.assertIn("[ResourceDragZoneParams_testns]", sections)
        self.assertFalse(any("JiggleMasks" in name for name in sections))

    def test_vis_publish_sections_present(self):
        _, sections, _ = self._emit()
        self.assertIn("[ResourceDragObjectVis_testns]", sections)
        self.assertIn("[CustomShaderDragVisPublish_testns]", sections)
        pub = "\n".join(sections["[CommandListDragVisPublish_testns]"])
        self.assertIn("x130 = $ssmtdrag_objvis_testns_0", pub)
        self.assertIn("run = CustomShaderDragVisPublish_testns", pub)

    def test_vis_globals_and_present_wiring(self):
        node, sections, comps = self._emit()
        node._emit_present_and_constants(sections, comps, "testns")
        const = "\n".join(sections["[Constants]"])
        present = "\n".join(sections["[Present]"])
        self.assertIn("global $ssmtdrag_objvis_testns_0 = 0", const)
        self.assertIn("pre run = CommandListDragVisPublish_testns", present)
        self.assertIn("post $ssmtdrag_objvis_testns_0 = 0", present)

    def test_global_object_oids_union(self):
        node = _make_node(self.mod)
        comps = [
            {"object_id_map": {"a": 0, "b": 2}},
            {"object_id_map": {"c": 5, "d": 1}},
        ]
        self.assertEqual(node._global_object_oids(comps), [0, 1, 2, 5])

    def test_vis_globals_use_global_oid_union_across_components(self):
        """回归：第二个组件起的 objvis flag 必须按全局 oid 声明/清零，
        不能按组件内 range 重数——曾致 b1870eee 的 oid 37-51 无 global 声明
        （透明布料显隐门控恒 0、命中被误杀），且 post 出现 0-14 重复。"""
        node, sections, _ = self._emit()
        comps = [
            {"comp_name": "abc123_43191", "object_count": 4,
             "object_id_map": {"A": 0, "B": 1, "C": 2, "D": 3}},
            {"comp_name": "xyz789_12345", "object_count": 3,
             "object_id_map": {"E": 4, "F": 5, "G": 6}},
        ]
        node._emit_present_and_constants(sections, comps, "testns")
        const = "\n".join(sections["[Constants]"])
        present = "\n".join(sections["[Present]"])
        for oid in range(7):
            self.assertEqual(
                const.count(f"global $ssmtdrag_objvis_testns_{oid} = 0"), 1,
                f"oid {oid} global 声明缺失或重复")
            self.assertEqual(
                present.count(f"post $ssmtdrag_objvis_testns_{oid} = 0"), 1,
                f"oid {oid} post 清零缺失或重复")

    def test_detect_variants_bind_triangle_ids_and_object_vis(self):
        _, sections, _ = self._emit()
        cn = "abc123_43191"
        p0 = "\n".join(sections[f"[CustomShaderDragDetect{cn}P0_testns]"])
        self.assertIn(f"cs-t7 = ResourceDragTriangleObjectIDs_{cn}P0_testns", p0)
        self.assertIn("cs-t8 = ResourceDragObjectVis_testns", p0)
        res = sections[f"[ResourceDragTriangleObjectIDs_{cn}P0_testns]"]
        self.assertIn("format = R32_UINT", res)

    def test_detect_shader_has_object_vis_gate(self):
        shader = (Path(__file__).resolve().parents[1]
                  / "Toolset" / "drag_interaction" / "rzm_object_detect.hlsl"
                  ).read_text(encoding="utf-8")
        self.assertIn("gTriangleObjectIDs : register(t7)", shader)
        self.assertIn("gObjectVis : register(t8)", shader)
        self.assertIn("gTriangleObjectIDs.Load(indexBase / 3u)", shader)
        self.assertIn("gObjectVis[visObjectId] < 0.5f", shader)

    def test_stable_zone_ids_migrate_and_preserve_high_ids(self):
        self.assertEqual(self.mod.MAX_ZONES, 256)
        explicit = self._zone_item(200)
        legacy = self._zone_item(-1)
        disabled = self._zone_item(17, enabled=False)
        node = _make_node(self.mod, zone_objects=[explicit, legacy, disabled])
        entries = node._collect_enabled_zone_entries()
        self.assertEqual([zone_id for zone_id, _ in entries], [0, 200])
        self.assertEqual(legacy.zone_id, 0)
        self.assertEqual(disabled.zone_id, 17)

        highest = self._zone_item(255)
        node = _make_node(self.mod, zone_objects=[highest])
        sections = _base_sections()
        comps = node._locate_components(sections, ["abc123"])
        node._emit_sections(sections, comps, "testns")
        self.assertIn("array = 256", sections["[ResourceDragPathProgressState_testns]"])
        jig = "\n".join(sections["[CustomShaderDragJiggleabc123_43191_testns]"])
        self.assertIn("y119 = 256", jig)

        all_items = [self._zone_item(zone_id) for zone_id in range(256)]
        node = _make_node(self.mod, zone_objects=all_items)
        all_entries = node._collect_enabled_zone_entries()
        self.assertEqual(len(all_entries), 256)
        self.assertEqual([zone_id for zone_id, _ in all_entries], list(range(256)))

    def test_zone_page_is_clamped_after_out_of_range_input_and_removal(self):
        # ZONES_PER_PAGE = 1：一页一个权重球
        node = _make_node(self.mod, zone_objects=[object() for _ in range(17)])
        object.__setattr__(node, "zone_page", 15)
        self.assertEqual(self.mod._zone_page_state(node), (15, 17))
        self.assertEqual(node.zone_page, 15)  # draw 路径只读，不在绘制中改 RNA
        self.assertEqual(self.mod._clamp_zone_page(node), (15, 17))
        self.assertEqual(node.zone_page, 15)

        node.zone_objects.pop()
        self.assertEqual(self.mod._clamp_zone_page(node), (15, 16))
        self.assertEqual(node.zone_page, 15)

        self.assertEqual(self.mod._clamp_zone_page(node, -10), (0, 16))

    def test_sparse_bake_keeps_four_strongest_zone_weights(self):
        import tempfile
        zone_ids = [0, 1, 2, 3, 4, 255]
        items = [self._zone_item(zone_id, weight=(index + 1) / 10.0) for index, zone_id in enumerate(zone_ids)]
        node = _make_node(self.mod, zone_objects=items)
        node._check_zone_radius_scale = lambda zones: False
        node._read_position_buf = lambda *args: np.zeros((3, 3), dtype=np.float32)
        node._get_reference_matrix_inv = lambda comp: None
        node._get_non_mirror_mirror = lambda: None
        node._buffer_dir = lambda sections, comp: "Meshes"
        node._evaluate_zone_field = lambda positions, empty, *args, **kwargs: np.full(
            positions.shape[0], empty.ssmt_drag_zone.brush_strength, dtype=np.float32
        )
        comp = {"vertex_count": 3, "base_name": "sample", "comp_name": "sample", "parts": []}

        with tempfile.TemporaryDirectory() as td:
            legacy_dir = Path(td) / "Meshes"
            legacy_dir.mkdir(parents=True)
            for index in range(3):
                (legacy_dir / f"sampleJiggleMasks{index}.buf").write_bytes(b"legacy")
            node._write_jiggle_masks(td, {}, comp, "testns")
            ids = np.fromfile(Path(td) / "Meshes" / "sampleJiggleZoneIDs.buf", dtype=np.uint32).reshape(-1, 4)
            weights = np.fromfile(Path(td) / "Meshes" / "sampleJiggleZoneWeights.buf", dtype=np.float32).reshape(-1, 4)
            self.assertFalse(any((legacy_dir / f"sampleJiggleMasks{index}.buf").exists() for index in range(3)))

        for row_ids, row_weights in zip(ids, weights):
            kept = {int(zone_id): float(weight) for zone_id, weight in zip(row_ids, row_weights)}
            self.assertEqual(set(kept), {2, 3, 4, 255})
            self.assertAlmostEqual(kept[255], 0.6, places=6)

    def test_sparse_bake_pads_zzmi_redirect_vertex_prefix(self):
        """ZZMI RedirectSO 的 VLR 含 3 顶点前缀，而基础 Position.buf 不含；
        烘焙必须在运行时索引空间补齐前缀，不能用 3 行 field 索引 6 行数组。"""
        import tempfile

        item = self._zone_item(0)
        node = _make_node(self.mod, zone_objects=[item])
        node._check_zone_radius_scale = lambda zones: False
        node._read_position_buf = lambda *args: np.zeros((3, 3), dtype=np.float32)
        node._get_reference_matrix_inv = lambda comp: None
        node._get_export_space_matrix = lambda: np.eye(4)
        node._get_non_mirror_mirror = lambda: None
        node._buffer_dir = lambda sections, comp: "Meshes"
        node._evaluate_zone_field = lambda positions, *args, **kwargs: np.ones(
            len(positions), dtype=np.float32
        )
        comp = {
            "vertex_count": 6,
            "vertex_base": 3,
            "base_name": "sample",
            "comp_name": "sample",
            "parts": [],
        }

        with tempfile.TemporaryDirectory() as td:
            node._write_jiggle_masks(td, {}, comp, "testns")
            ids = np.fromfile(
                Path(td) / "Meshes" / "sampleJiggleZoneIDs.buf", dtype=np.uint32
            ).reshape(-1, 4)
            weights = np.fromfile(
                Path(td) / "Meshes" / "sampleJiggleZoneWeights.buf", dtype=np.float32
            ).reshape(-1, 4)

        self.assertEqual(weights.shape, (6, 4))
        self.assertTrue(bool(np.all(weights[:3] == 0.0)))
        self.assertTrue(bool(np.all(ids[:3] == self.mod.INVALID_ZONE_ID)))
        self.assertTrue(bool(np.all(weights[3:, 0] == 1.0)))
        self.assertTrue(bool(np.all(ids[3:, 0] == 0)))

    def test_sparse_bake_includes_objects_even_when_propagation_off(self):
        """关闭沿表面扩散时，包含物体列表仍必须参与导出侧权重烘焙。"""
        import tempfile
        item = self._zone_item(0)
        settings = item.zone_object.ssmt_drag_zone
        settings.propagate = False
        settings.include_objects = [
            types.SimpleNamespace(object=types.SimpleNamespace(name="Body"))
        ]
        node = _make_node(self.mod, zone_objects=[item])
        node._check_zone_radius_scale = lambda zones: False
        node._read_position_buf = lambda *args: np.zeros((6, 3), dtype=np.float32)
        node._get_reference_matrix_inv = lambda comp: None
        node._get_export_space_matrix = lambda: np.eye(4)
        node._get_non_mirror_mirror = lambda: None
        node._buffer_dir = lambda sections, comp: "Meshes"
        triangles = np.array([[0, 1, 2], [3, 4, 5]], dtype=np.int64)
        tri_part_names = np.array(["Body", "Head"], dtype=object)
        node._read_component_triangles = lambda *args: (triangles, tri_part_names)

        def eval_field(positions, empty, *args, **kwargs):
            allowed = kwargs.get("allowed_mask")
            self.assertIsNotNone(allowed)
            np.testing.assert_array_equal(allowed, [True, True, True, False, False, False])
            return allowed.astype(np.float32)

        node._evaluate_zone_field = eval_field

        comp = {"vertex_count": 6, "base_name": "sample", "comp_name": "sample", "parts": []}
        with tempfile.TemporaryDirectory() as td:
            node._write_jiggle_masks(td, {}, comp, "testns")
            weights = np.fromfile(
                Path(td) / "Meshes" / "sampleJiggleZoneWeights.buf", dtype=np.float32
            ).reshape(-1, 4)

        self.assertTrue(bool(np.all(weights[:3, 0] > 0.0)))
        self.assertTrue(bool(np.all(weights[3:, 0] == 0.0)))

    def test_zone_field_applies_export_space_matrix_to_empty(self):
        node = _make_node(self.mod)
        settings = types.SimpleNamespace(brush_strength=1.0, brush_falloff_k=4.6)
        empty_matrix = np.eye(4)
        empty_matrix[:3, 3] = [0.069, -0.139, 1.143]
        empty = types.SimpleNamespace(name="SSMT_DragZone_0", matrix_world=empty_matrix)
        positions = np.asarray([[0.069, 1.143, 0.139]], dtype=np.float32)
        export_matrix = np.asarray([
            [1.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 1.0, 0.0],
            [0.0, -1.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ])

        field = node._evaluate_zone_field(
            positions,
            empty,
            settings,
            None,
            None,
            export_matrix=export_matrix,
        )

        np.testing.assert_allclose(field, [1.0])

    def test_sparse_bake_skips_component_when_all_zones_zero(self):
        import tempfile
        item = self._zone_item(0)
        node = _make_node(self.mod, zone_objects=[item])
        node._check_zone_radius_scale = lambda zones: False
        node._read_position_buf = lambda *args: np.zeros((3, 3), dtype=np.float32)
        node._get_reference_matrix_inv = lambda comp: None
        node._get_export_space_matrix = lambda: np.eye(4)
        node._get_non_mirror_mirror = lambda: None
        node._buffer_dir = lambda sections, comp: "Meshes"
        node._evaluate_zone_field = lambda *args, **kwargs: None
        comp = {"vertex_count": 3, "base_name": "sample", "comp_name": "sample", "parts": []}

        with tempfile.TemporaryDirectory() as td:
            # 全区域零权重 → 跳过该组件（返回 False），不报错也不写掩码
            self.assertFalse(node._write_jiggle_masks(td, {}, comp, "testns"))
            self.assertFalse((Path(td) / "Meshes" / "sampleJiggleZoneWeights.buf").exists())
            self.assertFalse((Path(td) / "Meshes" / "sampleJiggleZoneIDs.buf").exists())

    def test_bake_component_resources_skips_object_map_when_masks_skipped(self):
        import tempfile
        item = self._zone_item(0)
        node = _make_node(self.mod, zone_objects=[item])
        node._check_zone_radius_scale = lambda zones: False
        node._read_position_buf = lambda *args: np.zeros((3, 3), dtype=np.float32)
        node._get_reference_matrix_inv = lambda comp: None
        node._get_export_space_matrix = lambda: np.eye(4)
        node._get_non_mirror_mirror = lambda: None
        node._buffer_dir = lambda sections, comp: "Meshes"
        node._evaluate_zone_field = lambda *args, **kwargs: None
        comp = {"vertex_count": 3, "base_name": "sample", "comp_name": "sample", "parts": []}

        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(node._bake_component_resources(td, {}, comp, "testns"))
            # 组件被跳过时 ObjectMap / 掩码都不应生成
            self.assertFalse((Path(td) / "Meshes" / "sampleObjectMap.buf").exists())
            self.assertFalse((Path(td) / "Meshes" / "sampleJiggleZoneWeights.buf").exists())

    def test_bake_component_resources_writes_object_map_when_masks_written(self):
        import tempfile
        item = self._zone_item(0)
        node = _make_node(self.mod, zone_objects=[item])
        node._check_zone_radius_scale = lambda zones: False
        node._read_position_buf = lambda *args: np.zeros((3, 3), dtype=np.float32)
        node._get_reference_matrix_inv = lambda comp: None
        node._get_export_space_matrix = lambda: np.eye(4)
        node._get_non_mirror_mirror = lambda: None
        node._buffer_dir = lambda sections, comp: "Meshes"
        node._evaluate_zone_field = lambda *args, **kwargs: np.ones(3, dtype=np.float32)
        comp = {"vertex_count": 3, "base_name": "sample", "comp_name": "sample", "parts": []}

        with tempfile.TemporaryDirectory() as td:
            self.assertTrue(node._bake_component_resources(td, {}, comp, "testns"))
            self.assertTrue((Path(td) / "Meshes" / "sampleObjectMap.buf").exists())
            self.assertTrue((Path(td) / "Meshes" / "sampleJiggleZoneWeights.buf").exists())

    def test_zone_params_buffer_uses_stable_id_and_grabbable_flag(self):
        import tempfile
        item = self._zone_item(255, grabbable=False)
        settings = item.zone_object.ssmt_drag_zone
        settings.radius = 0.25
        settings.strength = 1.5
        settings.max_offset = 0.75
        settings.falloff = 2.0
        settings.damping = 0.8
        node = _make_node(self.mod, zone_objects=[item])
        with tempfile.TemporaryDirectory() as td:
            node._write_zone_resources(td, "testns")
            params = np.fromfile(Path(td) / "res" / "drag_interaction" / "ZoneParams_testns.buf", dtype=np.float32).reshape(-1, 4)
        self.assertEqual(params.shape, (512, 4))
        np.testing.assert_allclose(params[510], [0.25, 1.5, 0.75, 2.0])
        np.testing.assert_allclose(params[511], [0.8, 0.0, 0.0, 1.0])

    def test_sparse_zone_shaders_have_no_legacy_twelve_zone_clamp(self):
        shader_dir = REPO_ROOT / "Toolset" / "drag_interaction"
        detect = (shader_dir / "rzm_object_detect.hlsl").read_text(encoding="utf-8")
        jiggle = (shader_dir / "rzm_jiggle_interaction.hlsl").read_text(encoding="utf-8")
        screen = (shader_dir / "rzm_jiggle_screen_state.hlsl").read_text(encoding="utf-8")
        self.assertIn("Buffer<uint4>                      gJiggleZoneIDs", detect)
        self.assertIn("uint candidates[12]", detect)
        self.assertIn("Buffer<float4> ZoneParams", jiggle)
        self.assertIn("Buffer<float4> ZoneParams", screen)
        for source in (detect, jiggle, screen):
            self.assertNotIn("0.0, 11.0", source)
            self.assertNotIn("ZONE_STRENGTH_R2", source)

    def test_grab_gesture_modes(self):
        """三种抓取手势模式的 Present 判定行（回归：原作 COMBO 语义 左右键同按/X）。"""
        for gesture, expect in (
            ('LMB', "$ssmtdrag_lmb_down_testns == 1 || $ssmtdrag_x_down_testns == 1"),
            ('RMB', "$ssmtdrag_rmb_down_testns == 1 || $ssmtdrag_x_down_testns == 1"),
            ('COMBO', "($ssmtdrag_lmb_down_testns == 1 && $ssmtdrag_rmb_down_testns == 1) || $ssmtdrag_x_down_testns == 1"),
        ):
            node = _make_node(self.mod, grab_gesture=gesture)
            sections = _base_sections()
            comps = node._locate_components(sections, ["abc123"])
            node._emit_present_and_constants(sections, comps, "testns")
            present_text = "\n".join(sections["[Present]"])
            self.assertIn(expect, present_text, f"手势模式 {gesture} 的判定行缺失")

    def test_default_rmb_poke_pushes_into_mesh(self):
        node = _make_node(self.mod, grab_gesture='LMB', poke_gesture='RMB')
        sections = _base_sections()
        comps = node._locate_components(sections, ["abc123"])
        node._emit_present_and_constants(sections, comps, "testns")
        present = "\n".join(sections["[Present]"])

        self.assertIn(
            "if $ssmtdrag_rmb_prev_testns == 1 && $ssmtdrag_rmb_down_testns == 0\n"
            "\t\t$ssmtdrag_poke_sign_testns = -1",
            present,
        )
        self.assertNotIn(
            "if $ssmtdrag_lmb_prev_testns == 1 && $ssmtdrag_lmb_down_testns == 0\n"
            "\t\t$ssmtdrag_poke_sign_testns = 1",
            present,
        )

    def test_poke_gesture_can_select_lmb_or_both_buttons_and_always_pushes_inward(self):
        for gesture, expect_lmb, expect_rmb in (
            ('LMB', True, False),
            ('BOTH', True, True),
        ):
            node = _make_node(self.mod, grab_gesture='COMBO', poke_gesture=gesture)
            sections = _base_sections()
            comps = node._locate_components(sections, ["abc123"])
            node._emit_present_and_constants(sections, comps, "testns")
            present = "\n".join(sections["[Present]"])
            lmb_release = (
                "if $ssmtdrag_lmb_prev_testns == 1 && $ssmtdrag_lmb_down_testns == 0\n"
                "\t\t$ssmtdrag_poke_sign_testns = -1"
            )
            rmb_release = (
                "if $ssmtdrag_rmb_prev_testns == 1 && $ssmtdrag_rmb_down_testns == 0\n"
                "\t\t$ssmtdrag_poke_sign_testns = -1"
            )
            self.assertEqual(lmb_release in present, expect_lmb, gesture)
            self.assertEqual(rmb_release in present, expect_rmb, gesture)

    def test_bake_rt_matches_original(self):
        """BakeRT 必须与原作 ResourceLLBakeRT 一致（bind_flags/mode/mips/format 全齐）。"""
        _, sections, _ = self._emit()
        lines = sections["[ResourceDragBakeRT_testns]"]
        self.assertIn("bind_flags = render_target shader_resource", lines)
        self.assertIn("format = DXGI_FORMAT_R32G32B32A32_FLOAT", lines)
        self.assertIn("mode = mono", lines)
        self.assertIn("mips = 1", lines)
        self.assertIn("width = 8", lines)
        self.assertIn("height = 2", lines)
        # 非法关键字不得出现
        for line in lines:
            self.assertFalse(line.strip().startswith("uav"), line)
            self.assertFalse(line.strip().startswith("render_target ="), line)

    def test_boot_clear_present_and_idempotent(self):
        node, sections, comps = self._emit()
        pin = "\n".join(sections["[CommandListDragPinDetected_testns]"])
        self.assertIn("if $ssmtdrag_booted_testns == 0", pin)
        self.assertIn("clear = ResourceDragDetectID_testns 0.0", pin)
        # 二次 emit 幂等（setdefault + 段存在跳过）
        before = {k: list(v) for k, v in sections.items()}
        node._emit_sections(sections, comps, "testns")
        for k, v in before.items():
            self.assertEqual(sections[k], v, f"段 {k} 二次 emit 后变化")

        self.assertIn(self.mod.DRAG_TAIL_MARKER, sections)
        self.assertEqual(sum(key == self.mod.DRAG_TAIL_MARKER for key in sections), 1)

    def test_ui_drag_bridge_exports_detected_and_zone_globals(self):
        """模型区域拖动 UI 依赖稳定的 GPU -> INI 全局变量桥接。"""
        node, sections, comps = self._emit()
        node._emit_present_and_constants(sections, comps, "testns")

        constants = "\n".join(sections["[Constants]"])
        present = "\n".join(sections["[Present]"])
        readback = "\n".join(sections.get("[CommandListDragUIReadback_testns]", []))
        self.assertIn("global $ssmtdrag_ui_detected_testns = -1", constants)
        self.assertIn("global $ssmtdrag_ui_zone_testns = -1", constants)
        self.assertIn(
            "store = $ssmtdrag_ui_detected_testns, ResourceDragPinnedDetectID_testns, 0",
            readback,
        )
        self.assertIn(
            "store = $ssmtdrag_ui_zone_testns, ResourceDragZoneOut_testns, 0",
            readback,
        )
        self.assertIn("if $ssmtdrag_ui_detected_testns < 0 || $ObjectDetectAllowed_testns != 1", readback)
        self.assertIn("$ssmtdrag_ui_detected_testns = -1", readback)
        self.assertIn("$ssmtdrag_ui_zone_testns = -1", readback)
        self.assertIn("type = StructuredBuffer", sections["[ResourceDragPinnedDetectID_testns]"])
        self.assertIn("stride = 4", sections["[ResourceDragPinnedDetectID_testns]"])
        self.assertIn("array = 2", sections["[ResourceDragPinnedDetectID_testns]"])
        self.assertIn("type = RWBuffer", sections["[ResourceDragZoneOut_testns]"])
        self.assertIn("type = StructuredBuffer", sections["[ResourceDragPinnedDetectInfo_testns]"])
        self.assertIn("stride = 16", sections["[ResourceDragPinnedDetectInfo_testns]"])

        # 旧版 Present 已存在时仍可幂等补齐桥接，而不是被提前 return 跳过。
        legacy_sections = _base_sections()
        legacy_sections["[Present]"] = [
            "; --- DRAG PRESENT BEGIN ---",
            "post $ssmtdrag_drawn_testns = 0",
            "; --- DRAG PRESENT END ---",
        ]
        node._emit_present_and_constants(legacy_sections, comps, "testns")
        readback = "\n".join(legacy_sections.get("[CommandListDragUIReadback_testns]", []))
        self.assertEqual(readback.count("DRAG UI BRIDGE BEGIN"), 1)
        node._emit_present_and_constants(legacy_sections, comps, "testns")
        self.assertEqual(readback.count("DRAG UI BRIDGE BEGIN"), 1)

    def test_hand_cursor_gate_requires_armed_mode(self):
        """手型光标门控必须含 $ssmtdrag_mode（Alt 臂动）：检测只在臂动时刷新命中，
        松开 Alt 后命中数据是陈旧的——若无臂动门控，手会停留在松开前的命中点不消失。"""
        node, sections, comps = self._emit(enable_hand_cursor=True)
        node._emit_present_and_constants(sections, comps, "testns")
        present = sections["[Present]"]
        # 门控行同时包含模式开关、臂动标志与 drawn
        gate_line = next(
            line for line in present
            if line.strip().startswith("if $ssmtdrag_drag_enabled_testns >= 1")
            and "$ssmtdrag_mode_testns == 1" in line
            and "$ssmtdrag_drawn_testns == 1" in line
        )
        idx = present.index(gate_line)
        self.assertIn("run = CustomShaderDragUpdateJiggleCursorPreview_testns", present[idx + 1])

    def test_drag_runtime_mode_one_keeps_detection_but_disables_deformation(self):
        node, sections, comps = self._emit(drag_system_mode_default=1, enable_hand_cursor=True)
        node._emit_present_and_constants(sections, comps, "testns")

        constants = "\n".join(sections["[Constants]"])
        present = "\n".join(sections["[Present]"])
        pin = "\n".join(sections["[CommandListDragPinDetected_testns]"])
        hook = "\n".join(node._build_hook_block(comps[0], 0, "testns"))
        self.assertIn("global persist $ssmtdrag_drag_enabled_testns = 1", constants)
        self.assertIn("if $ssmtdrag_drag_enabled_testns < 2", present)
        self.assertIn("$isMouseButtonDown = 0", present)
        self.assertIn("$ssmtdrag_poke_sign_testns = 0", present)
        self.assertIn("$ssmtdrag_rmb_lone_hold_testns = 0", present)
        self.assertIn("$ssmtdrag_drag_enabled_testns >= 1", pin)
        self.assertIn("run = CustomShaderDragPinDetected_testns", pin)
        self.assertIn("$ssmtdrag_drag_enabled_testns >= 1", hook)
        self.assertIn("$ssmtdrag_drag_enabled_testns >= 2", hook)

    def test_drag_runtime_mode_zero_and_custom_variables_disable_entire_system(self):
        node, sections, comps = self._emit(
            drag_system_mode_default=0,
            drag_mode_variable_name="$custom_drag_mode",
            ui_detected_variable_name="$custom_hit_id",
            ui_zone_variable_name="$custom_zone_id",
        )
        node._emit_present_and_constants(sections, comps, "testns")

        constants = "\n".join(sections["[Constants]"])
        present = "\n".join(sections["[Present]"])
        readback = "\n".join(sections.get("[CommandListDragUIReadback_testns]", []))
        pin = "\n".join(sections["[CommandListDragPinDetected_testns]"])
        hook = "\n".join(node._build_hook_block(comps[0], 0, "testns"))
        self.assertIn("global persist $custom_drag_mode = 0", constants)
        self.assertIn("global $custom_hit_id = -1", constants)
        self.assertIn("global $custom_zone_id = -1", constants)
        self.assertNotIn("global persist $ssmtdrag_drag_enabled_testns", constants)
        self.assertIn("if $custom_drag_mode < 1", present)
        self.assertIn("if $custom_drag_mode >= 1", pin)
        self.assertIn("if $custom_drag_mode >= 1", hook)
        self.assertIn("if $custom_drag_mode >= 2", hook)
        self.assertIn("store = $custom_hit_id, ResourceDragPinnedDetectID_testns, 0", readback)
        self.assertIn("store = $custom_zone_id, ResourceDragZoneOut_testns, 0", readback)

    def test_drag_runtime_switch_defaults_on_and_upgrades_legacy_present(self):
        node, sections, comps = self._emit()
        legacy_present = [
            "; --- DRAG PRESENT BEGIN ---",
            "if $ssmtdrag_mode_testns == 1",
            "\tpre run = CommandListDragPinDetected_testns",
            "endif",
            "post $ssmtdrag_drawn_testns = 0",
            "; --- DRAG PRESENT END ---",
        ]
        sections["[Present]"] = legacy_present
        node._emit_present_and_constants(sections, comps, "testns")
        constants = "\n".join(sections["[Constants]"])
        present = "\n".join(sections["[Present]"])
        self.assertIn("global persist $ssmtdrag_drag_enabled_testns = 2", constants)
        self.assertEqual(present.count("DRAG INTERACTION GATE BEGIN"), 1)
        self.assertLess(present.index("DRAG INTERACTION GATE BEGIN"), present.index("pre run = CommandListDragPinDetected_testns"))
        node._emit_present_and_constants(sections, comps, "testns")
        self.assertEqual("\n".join(sections["[Present]"]).count("DRAG INTERACTION GATE BEGIN"), 1)

    def test_legacy_boolean_drag_enabled_migrates_to_three_level_mode(self):
        mod = self.mod
        legacy_off = _make_node(
            mod,
            drag_enabled_default=False,
            drag_system_mode_default=2,
            drag_mode_initialized=False,
        )
        self.assertEqual(legacy_off._default_drag_system_mode(), 1)
        self.assertEqual(legacy_off.drag_system_mode_default, 1)
        self.assertTrue(legacy_off.drag_mode_initialized)

        legacy_on = _make_node(
            mod,
            drag_enabled_default=True,
            drag_system_mode_default=0,
            drag_mode_initialized=False,
        )
        self.assertEqual(legacy_on._default_drag_system_mode(), 2)
        self.assertEqual(legacy_on.drag_system_mode_default, 2)

    def test_no_zone_fallback_zone0_remains_grabbable(self):
        import tempfile
        node = _make_node(self.mod)
        with tempfile.TemporaryDirectory() as td:
            node._write_zone_resources(td, "testns")
            params = np.fromfile(Path(td) / "res" / "drag_interaction" / "ZoneParams_testns.buf", dtype=np.float32).reshape(-1, 4)
        self.assertEqual(params.shape, (2, 4))
        self.assertEqual(params[1].tolist(), [0.0, 1.0, 0.0, 1.0])

    def test_redirect_fallback_keeps_stub_prefix_invalid(self):
        import tempfile

        node = _make_node(self.mod)
        comp = {
            "base_name": "sample",
            "comp_name": "sample",
            "vertex_base": 3,
            "base_resource": "ResourceSamplePosition",
        }
        sections = {"[ResourceSamplePosition]": ["filename = Meshes0000/sample-Position.buf"]}
        with tempfile.TemporaryDirectory() as td:
            node._write_masks_fallback(
                td,
                sections,
                comp,
                vertex_count=6,
                fallback_zone_id=2,
                vertex_base=3,
            )
            ids = np.fromfile(Path(td) / "Meshes0000" / "sampleJiggleZoneIDs.buf", dtype=np.uint32)
            weights = np.fromfile(Path(td) / "Meshes0000" / "sampleJiggleZoneWeights.buf", dtype=np.float32)
        self.assertTrue(np.all(ids[: 3 * 4] == self.mod.INVALID_ZONE_ID))
        self.assertTrue(np.all(weights[: 3 * 4] == 0.0))
        self.assertTrue(np.all(ids[3 * 4 :: 4] == 2))
        self.assertTrue(np.all(weights[3 * 4 :: 4] == 1.0))

    def test_all_used_globals_declared(self):
        """发射的所有段中被引用的 $ 变量必须在 [Constants] 有 global 声明。
        回归：$ssmtdrag_viewport_valid 曾未声明——3DMigoto 中变量跨 run= 命令列表
        边界传递必须声明 global，否则子列表置 1 后调用方仍读 0 → 视口恒无效 →
        cursor 恒 (-1,-1) → 检测永不命中且手型光标锚定屏幕外（导出模组无效果的根因）。
        本测试覆盖该 bug 的整个类别，防止任何变量漏声明。"""
        import re as _re
        builtin = {"$time", "$cursor_x", "$cursor_y", "$cursor_window_x", "$cursor_window_y",
                   "$cursor_screen_x", "$cursor_screen_y", "$window_width", "$window_height",
                   "$res_width", "$res_height", "$rt_width", "$rt_height", "$draw_type",
                   "$DRAW_TYPE", "$INDEX_COUNT", "$FIRST_INDEX", "$FIRST_VERTEX",
                   "$INSTANCE_COUNT", "$FIRST_INSTANCE"}
        # 同时覆盖 enable_hand_cursor=True（用户实际导出配置）以纳入手部 globals。
        # 注意 [Constants] 全局由 _emit_present_and_constants 写入，必须两步都调。
        node, sections, comps = self._emit(enable_hand_cursor=True)
        node._emit_present_and_constants(sections, comps, "testns")
        declared = set()
        for l in sections.get("[Constants]", []):
            m = _re.match(r"\s*global(?:\s+persist)?\s+(\$\w+)", l)
            if m:
                declared.add(m.group(1))
        undeclared = {}
        for name, lines in sections.items():
            locals_ = set()
            for l in lines:
                m = _re.match(r"\s*local\s+(\$\w+)", l)
                if m:
                    locals_.add(m.group(1))
            for l in lines:
                s = l.strip()
                if not s or s.startswith(";"):
                    continue
                for v in _re.findall(r"\$\w+", s):
                    if v in locals_ or v in builtin:
                        continue
                    if v not in declared:
                        undeclared.setdefault(v, []).append(name)
        self.assertFalse(undeclared, f"未声明变量: {undeclared}")


class DragNodeReexportLatchMigrationTests(unittest.TestCase):
    """D1 回归：旧格式（锁存前）ini 重导出必须升级锁存族段。

    旧 drive/var_sync CS 段无 cs-u5 绑定、旧 PinDetected 段无 latch 清零；
    读入时按内容特征剥离（_remove_existing_legacy_shapekey_sections），
    发射侧无条件重发（与 jiggle 同等待遇）→ 重导出后锁存链存活：
    cs-u5 绑定存在、boot/失臂清零生效，防 F1 假绑/F2 复活回潮。
    """

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()

    def _node_with_sk_consumer(self):
        node = _make_node(self.mod, enable_shapekey_drive=True)
        node.id_data = types.SimpleNamespace(nodes=[
            DragNodeEmitTests._fake_sk_node([("A", 0, "-1", 2), ("B", 0, "0", 1)]),
        ])
        return node

    def test_read_ini_strips_legacy_shapekey_sections(self):
        """读入剥离：无 cs-u5 绑定的旧 drive/var_sync 段与无 latch 清零的旧
        PinDetected 段必须被删；已含锁存标记的新格式段保留。"""
        node = _make_node(self.mod)
        import tempfile
        old_content = "\n".join([
            "[Constants]",
            "global $active = 0",
            "[CustomShaderDragShapeKeyDrive_testns]",
            "cs = res/drag_interaction/rzm_shapekey_drive.hlsl",
            "cs-u0 = ResourceDragShapeKeyDrive_testns",
            "cs-u4 = ResourceDragShapeKeyClickCountF_testns",
            "dispatch = 1, 1, 1",
            "[CustomShaderDragShapeKeyVarSync_testns]",
            "cs = res/drag_interaction/rzm_shapekey_var_sync.hlsl",
            "cs-u4 = ResourceDragShapeKeyZoneActive_testns",
            "dispatch = 1, 1, 1",
            "[CommandListDragPinDetected_testns]",
            "if $ssmtdrag_booted_testns == 0",
            "\tclear = ResourceDragShapeKeyDrive_testns 0.0",
            "\t$ssmtdrag_booted_testns = 1",
            "endif",
        ])
        with tempfile.TemporaryDirectory() as td:
            ini_path = Path(td) / "old.ini"
            ini_path.write_text(old_content, encoding="utf-8")
            sections, _tail, _driver = node._read_ini_to_ordered_dict(str(ini_path))
        self.assertNotIn("[CustomShaderDragShapeKeyDrive_testns]", sections)
        self.assertNotIn("[CustomShaderDragShapeKeyVarSync_testns]", sections)
        self.assertNotIn("[CommandListDragPinDetected_testns]", sections)

        # 当前形态（含 cs-u5 / latch 清零）不剥离
        new_content = old_content.replace(
            "cs-u0 = ResourceDragShapeKeyDrive_testns",
            "cs-u0 = ResourceDragShapeKeyDrive_testns\n"
            "cs-u5 = ResourceDragShapeKeyDragLatch_testns",
        ).replace(
            "cs-u4 = ResourceDragShapeKeyZoneActive_testns",
            "cs-u4 = ResourceDragShapeKeyZoneActive_testns\n"
            "cs-u5 = ResourceDragShapeKeyDragLatch_testns",
        ).replace(
            "\tclear = ResourceDragShapeKeyDrive_testns 0.0",
            "\tclear = ResourceDragShapeKeyDrive_testns 0.0\n"
            "\tclear = ResourceDragShapeKeyDragLatch_testns 0.0",
        )
        with tempfile.TemporaryDirectory() as td:
            ini_path = Path(td) / "new.ini"
            ini_path.write_text(new_content, encoding="utf-8")
            sections, _tail, _driver = node._read_ini_to_ordered_dict(str(ini_path))
        self.assertIn("[CustomShaderDragShapeKeyDrive_testns]", sections)
        self.assertIn("[CustomShaderDragShapeKeyVarSync_testns]", sections)
        self.assertIn("[CommandListDragPinDetected_testns]", sections)

    def test_latch_family_sections_unconditional_reemit(self):
        """无条件重发：即便 sections 里已存在旧形态段（绕过读入剥离路径），
        发射侧也必须覆盖为携带 cs-u5 绑定的当前形态。"""
        sections = _base_sections()
        sections["[CustomShaderDragShapeKeyDrive_testns]"] = [
            "cs = res/drag_interaction/rzm_shapekey_drive.hlsl",
            "cs-u0 = ResourceDragShapeKeyDrive_testns",
            "dispatch = 1, 1, 1",
        ]
        sections["[CustomShaderDragShapeKeyVarSync_testns]"] = [
            "cs = res/drag_interaction/rzm_shapekey_var_sync.hlsl",
            "dispatch = 1, 1, 1",
        ]
        sections["[CommandListDragPinDetected_testns]"] = [
            "if $ssmtdrag_booted_testns == 0",
            "\tclear = ResourceDragShapeKeyDrive_testns 0.0",
            "\t$ssmtdrag_booted_testns = 1",
            "endif",
        ]
        node = self._node_with_sk_consumer()
        comps = node._locate_components(sections, ["abc123"])
        node._emit_sections(sections, comps, "testns")

        cs = "\n".join(sections["[CustomShaderDragShapeKeyDrive_testns]"])
        self.assertIn("cs-u5 = ResourceDragShapeKeyDragLatch_testns", cs)
        self.assertIn("post cs-u5 = null", cs)
        vs = "\n".join(sections["[CustomShaderDragShapeKeyVarSync_testns]"])
        self.assertIn("cs-u5 = ResourceDragShapeKeyDragLatch_testns", vs)
        pin = "\n".join(sections["[CommandListDragPinDetected_testns]"])
        self.assertIn("clear = ResourceDragShapeKeyDragLatch_testns 0.0", pin)

    def test_old_ini_reexport_latch_chain_alive(self):
        """端到端回归（验收场景）：旧格式 ini → 读入剥离 → 重导出 →
        锁存链存活（cs-u5 绑定存在、boot/失臂清零生效、latch 资源落盘）。"""
        import tempfile
        old_drive = "\n".join([
            "cs = res/drag_interaction/rzm_shapekey_drive.hlsl",
            "x76 = $ssmtdrag_delta_time_testns",
            "z77 = $ssmtdrag_drag_enabled_testns",
            "w77 = $ssmtdrag_lmb_down_testns",
            "x78 = $ssmtdrag_x_down_testns",
            "x79 = $ssmtdrag_shapekey_dy_testns",
            "y79 = $ssmtdrag_shapekey_dx_testns",
            "x80 = 0.02",
            "cs-u0 = ResourceDragShapeKeyDrive_testns",
            "cs-u1 = ResourceDragShapeKeyDir_testns",
            "cs-u2 = ResourceDragShapeKeyClickCount_testns",
            "cs-u3 = ResourceDragShapeKeyActiveDir_testns",
            "cs-u4 = ResourceDragShapeKeyClickCountF_testns",
            "dispatch = 1, 1, 1",
            "post cs-u0 = null",
            "post cs-u1 = null",
            "post cs-u2 = null",
            "post cs-u3 = null",
            "post cs-u4 = null",
        ])
        old_pin = "\n".join([
            "if $ssmtdrag_booted_testns == 0",
            "\tclear = ResourceDragDetectID_testns 0.0",
            "\tclear = ResourceDragShapeKeyDrive_testns 0.0",
            "\tclear = ResourceDragShapeKeyDir_testns 0.0",
            "\t$ssmtdrag_booted_testns = 1",
            "endif",
            "if $ssmtdrag_drag_enabled_testns >= 1 && $ssmtdrag_mode_testns == 1",
            "\t$ObjectDetectAllowed_testns = 1",
            "\trun = CustomShaderDragShapeKeyDrive_testns",
            "else",
            "\t$ObjectDetectAllowed_testns = 0",
            "endif",
        ])
        content = "\n".join([
            "[Constants]",
            "global $active = 0",
            "[TextureOverride_abc123_abc123-43191_VertexLimitRaise]",
            "hash = abc123",
            "override_byte_stride = 40",
            "override_vertex_count = 14078",
            "uav_byte_stride = 4",
            "[TextureOverride_abc123_abc123-43191A]",
            "hash = abc123",
            "match_first_index = 0",
            "ib = Resourceabc123-43191AIB",
            "drawindexed = 52688, 0, 0",
            "[Resource_abc123_Position]",
            "type = Buffer",
            "stride = 40",
            "filename = Meshes/abc123-43191-Position.buf",
            "[Resourceabc123-43191AIB]",
            "type = Buffer",
            "format = DXGI_FORMAT_R32_UINT",
            "[CustomShaderDragShapeKeyDrive_testns]",
            old_drive,
            "[CommandListDragPinDetected_testns]",
            old_pin,
            "[Present]",
        ])
        with tempfile.TemporaryDirectory() as td:
            ini_path = Path(td) / "old.ini"
            ini_path.write_text(content, encoding="utf-8")
            node = self._node_with_sk_consumer()
            sections, _tail, _driver = node._read_ini_to_ordered_dict(str(ini_path))
            # 旧段读入即剥离（无 cs-u5 / 无 latch 清零）
            self.assertNotIn("[CustomShaderDragShapeKeyDrive_testns]", sections)
            self.assertNotIn("[CommandListDragPinDetected_testns]", sections)
            comps = node._locate_components(sections, ["abc123"])
            self.assertTrue(comps)
            node._emit_sections(sections, comps, "testns")
            node._emit_present_and_constants(sections, comps, "testns")
            # 锁存链存活：cs-u5 绑定 + latch 资源落盘
            cs = "\n".join(sections["[CustomShaderDragShapeKeyDrive_testns]"])
            self.assertIn("cs-u5 = ResourceDragShapeKeyDragLatch_testns", cs)
            self.assertIn("post cs-u5 = null", cs)
            vs = "\n".join(sections["[CustomShaderDragShapeKeyVarSync_testns]"])
            self.assertIn("cs-u5 = ResourceDragShapeKeyDragLatch_testns", vs)
            self.assertEqual(
                sections["[ResourceDragShapeKeyDragLatch_testns]"],
                ["type = RWBuffer", "format = R32_FLOAT", "array = 1"],
            )
            # boot 清零 + 失臂 else 清零生效
            pin = sections["[CommandListDragPinDetected_testns]"]
            boot_idx = next(
                i for i, line in enumerate(pin)
                if "$ssmtdrag_booted_testns == 0" in line
            )
            self.assertIn(
                "clear = ResourceDragShapeKeyDragLatch_testns 0.0",
                "\n".join(pin[boot_idx:]),
            )
            disarm_idx = next(
                i for i, line in enumerate(pin)
                if line == "\t$ObjectDetectAllowed_testns = 0"
            )
            self.assertEqual(
                pin[disarm_idx + 1],
                "\tclear = ResourceDragShapeKeyDragLatch_testns 0.0",
            )
            # Present 终态 else（G1：模式 1→0 直跳清锁存）
            present = "\n".join(sections["[Present]"])
            self.assertIn(
                "else\n\tclear = ResourceDragShapeKeyDragLatch_testns 0.0\nendif",
                present,
            )


class DragNodeInjectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()

    def test_hook_insert_position_after_ib_before_run(self):
        node = _make_node(self.mod)
        sections = _base_sections()
        comp = node._locate_components(sections, ["abc123"])[0]
        node._inject_draw_hooks(sections, comp, "testns")
        lines = sections["[TextureOverride_abc123_abc123-43191A]"]
        ib_idx = next(i for i, l in enumerate(lines) if l.strip().startswith("ib ="))
        hook_idx = next(i for i, l in enumerate(lines) if "DRAG HOOK BEGIN" in l)
        run_idx = next(i for i, l in enumerate(lines) if l.strip().startswith("run ="))
        draw_idx = next(i for i, l in enumerate(lines) if l.strip().startswith("drawindexed ="))
        self.assertTrue(ib_idx < hook_idx < run_idx < draw_idx)

    def test_hook_skip_ib_null_and_idempotent(self):
        node = _make_node(self.mod)
        sections = _base_sections()
        comp = node._locate_components(sections, ["abc123"])[0]
        node._inject_draw_hooks(sections, comp, "testns")
        # ib=null 段未被收集为 part，故不含 hook
        skip_lines = sections["[TextureOverride_abc123_abc123-43191Skip]"]
        self.assertFalse(any("DRAG HOOK" in l for l in skip_lines))
        # 幂等
        node._inject_draw_hooks(sections, comp, "testns")
        a_lines = sections["[TextureOverride_abc123_abc123-43191A]"]
        self.assertEqual(sum(1 for l in a_lines if "DRAG HOOK BEGIN" in l), 1)

    def test_vis_flag_injected_inside_draw_branch(self):
        node = _make_node(self.mod)
        sections = _base_sections()
        section_name = "[TextureOverride_abc123_abc123-43191A]"
        sections[section_name][-1:] = [
            "if $swapkey4 == 2",
            "    drawindexed = 52688, 0, 0",
            "endif",
        ]
        comp = node._locate_components(sections, ["abc123"])[0]
        node._inject_draw_hooks(sections, comp, "testns")

        lines = sections[section_name]
        flag_idx = next(
            i for i, line in enumerate(lines)
            if line.strip().startswith("$ssmtdrag_objvis_")
        )
        draw_idx = next(
            i for i, line in enumerate(lines)
            if line.strip().startswith("drawindexed =")
        )
        if_idx = next(
            i for i, line in enumerate(lines)
            if line.strip().startswith("if $swapkey4 == 2")
        )
        # flag 行位于分支 if 与绘制行之间（分支执行才置位）
        self.assertLess(if_idx, flag_idx)
        self.assertLess(flag_idx, draw_idx)
        oid = comp["object_id_map"].get("{}#0".format(section_name))
        self.assertIsNotNone(oid)
        self.assertIn(f"$ssmtdrag_objvis_testns_{oid} = 1", lines[flag_idx])

    def test_vis_flag_injection_idempotent(self):
        node = _make_node(self.mod)
        sections = _base_sections()
        section_name = "[TextureOverride_abc123_abc123-43191A]"
        sections[section_name][-1:] = [
            "if $swapkey4 == 2",
            "    drawindexed = 52688, 0, 0",
            "endif",
        ]
        comp = node._locate_components(sections, ["abc123"])[0]
        node._inject_draw_hooks(sections, comp, "testns")
        first = sum(
            1 for line in sections[section_name]
            if line.strip().startswith("$ssmtdrag_objvis_")
        )
        node._inject_draw_hooks(sections, comp, "testns")
        second = sum(
            1 for line in sections[section_name]
            if line.strip().startswith("$ssmtdrag_objvis_")
        )
        self.assertEqual(first, 1)
        self.assertEqual(first, second)

    def test_cross_ib_source_and_target_hooks_follow_their_mesh_prefixes(self):
        node = _make_node(self.mod)
        sections = _cross_ib_sections()
        components = node._locate_components(sections, ["sourcehash", "targethash"])

        node._emit_sections(sections, components, "testns")
        for comp in components:
            node._inject_draw_hooks(sections, comp, "testns")

        lines = sections["[TextureOverride_targethash_0]"]
        target_comment = lines.index("; [mesh:LOD0.targethash-300-0.Target_copy] [vertex_count:200]")
        source_comment = lines.index("; [mesh:LOD0.sourcehash-120-0.SourceA_copy] [vertex_count:80]")
        target_hook = next(i for i, line in enumerate(lines) if "DRAG HOOK BEGIN targethashP0_testns" in line)
        source_hook = next(i for i, line in enumerate(lines) if "DRAG HOOK BEGIN sourcehashP0_testns" in line)

        self.assertLess(target_hook, target_comment)
        self.assertLess(target_comment, source_hook)
        self.assertLess(source_hook, source_comment)
        self.assertEqual(sum("DRAG HOOK BEGIN" in line for line in lines), 2)

    def test_full_regeneration_cycle_keeps_one_tail_and_fresh_hooks(self):
        import tempfile

        node = _make_node(self.mod)
        initial_sections = _base_sections()
        section_name = "[TextureOverride_abc123_abc123-43191A]"
        initial_sections[section_name][-1:] = [
            "drawindexed = 53994, 0, 0",
            "drawindexed = 3618, 53994, 0",
        ]

        with tempfile.TemporaryDirectory() as td:
            ini_path = Path(td) / "test.ini"
            node._write_ordered_dict_to_ini(initial_sections, str(ini_path))

            for _generation in range(2):
                sections, preserved_tail, _preserved_driver = node._read_ini_to_ordered_dict(str(ini_path))
                comps = node._locate_components(sections, ["abc123"])
                node._emit_sections(sections, comps, "testns")
                for comp in comps:
                    node._inject_draw_hooks(sections, comp, "testns")
                node._emit_present_and_constants(sections, comps, "testns")
                node._write_ordered_dict_to_ini(sections, str(ini_path), preserved_tail)

                output = ini_path.read_text(encoding="utf-8")
                self.assertEqual(output.count(self.mod.DRAG_TAIL_MARKER), 1)
                self.assertEqual(output.count("DRAG HOOK BEGIN"), 2)
                self.assertEqual(output.count("DRAG HOOK END"), 2)
                self.assertEqual(output.count("[ResourceDragDetectID_testns]"), 1)
                # R5 合并后：8 组采样参数经 [Constants] global 迭代变量推导，
                # 不再以字面量逐段出现；bake 段仍 8 次 run 同一 sample 段
                self.assertIn("drawindexed = 1, $ssmtdrag_bake_off_abc123_43191P0_testns, 0", output)
                self.assertEqual(output.count("run = CustomShaderDragBakeSample_abc123_43191P0_testns"), 8)
                self.assertEqual(output.count("CustomShaderDragBakeSample1_abc123_43191P0_testns"), 0)


class DragNodePreviewTests(unittest.TestCase):
    """权重预览模块：节点收集过滤 + 签名变化检测（不触 GPU 绘制路径）。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()

    def setUp(self):
        # t12 需求变更：预览只显示选中的区域空物体——签名/重建路径依赖
        # bpy.context.selected_objects，本类统一桩为「无选中」（各用例按需覆写）
        self._orig_context = getattr(self.mod.bpy, "context", None)
        self.mod.bpy.context = types.SimpleNamespace(
            active_object=None, selected_objects=())

    def tearDown(self):
        self.mod.bpy.context = self._orig_context

    def _make_zone_empty(self, name="SSMT_DragZone_0", loc=(0.0, 0.0, 0.0), scale=1.0,
                         enabled=True, brush_strength=1.0, brush_falloff_k=4.6):
        m = np.eye(4)
        m[0, 3], m[1, 3], m[2, 3] = loc
        m[0, 0] = m[1, 1] = m[2, 2] = scale
        settings = types.SimpleNamespace(
            enabled=enabled, brush_strength=brush_strength, brush_falloff_k=brush_falloff_k)
        return types.SimpleNamespace(name=name, matrix_world=m, ssmt_drag_zone=settings)

    def _make_preview_node(self, mod, tree_name="Tree", node_name="Drag",
                           weights=True, target=None, collection=None, zone_empties=()):
        node = _make_node(mod)
        object.__setattr__(node, "preview_weights", weights)
        object.__setattr__(node, "preview_target", target)
        object.__setattr__(node, "preview_collection", collection)
        object.__setattr__(node, "id_data", types.SimpleNamespace(name=tree_name))
        object.__setattr__(node, "name", node_name)
        object.__setattr__(node, "zone_objects",
                           [types.SimpleNamespace(zone_object=e) for e in zone_empties])
        return node

    def test_collect_filters_enabled_nodes_with_target(self):
        mod = self.mod
        bpy = mod.bpy  # 模块加载时安装的 stub 实例（sys.modules["bpy"] 可能已被后续加载重建）
        target = types.SimpleNamespace(name="Mesh", matrix_world=np.eye(4),
                                       data=types.SimpleNamespace(vertices=range(100)))
        ok_node = self._make_preview_node(mod, target=target, zone_empties=[self._make_zone_empty()])
        off_node = self._make_preview_node(mod, tree_name="Tree2", node_name="DragOff",
                                           weights=False, target=target)
        no_target = self._make_preview_node(mod, tree_name="Tree3", node_name="DragNoT", weights=True)
        other = types.SimpleNamespace(bl_idname="SSMTNode_PostProcess_Other",
                                      preview_weights=True, preview_target=target)
        bpy.data.node_groups = [
            types.SimpleNamespace(nodes=[ok_node, other]),
            types.SimpleNamespace(nodes=[off_node, no_target]),
        ]
        found = mod._collect_preview_nodes()
        self.assertEqual([(n.id_data.name, n.name) for n in found],
                         [(tree_name, node_name) for tree_name, node_name in [("Tree", "Drag")]])
        self.assertIs(found[0], ok_node)

    def test_signature_sensitive_to_matrix_and_brush(self):
        mod = self.mod
        ctx = mod.bpy.context
        target = types.SimpleNamespace(name="Mesh", matrix_world=np.eye(4),
                                       data=types.SimpleNamespace(vertices=range(100)))
        empty = self._make_zone_empty()
        node = self._make_preview_node(mod, target=target, zone_empties=[empty])
        ctx.selected_objects = [empty]  # t12：区域空物体须被选中才进签名
        base = mod._preview_signature(node)
        # 移动空物体 → 签名变化
        moved = self._make_zone_empty(loc=(1.0, 0.0, 0.0))
        node2 = self._make_preview_node(mod, tree_name="Tree", node_name="Drag",
                                        target=target, zone_empties=[moved])
        ctx.selected_objects = [moved]
        self.assertNotEqual(base, mod._preview_signature(node2))
        # 改 brush_strength → 签名变化
        stronger = self._make_zone_empty(brush_strength=2.0)
        node3 = self._make_preview_node(mod, tree_name="Tree", node_name="Drag",
                                        target=target, zone_empties=[stronger])
        ctx.selected_objects = [stronger]
        self.assertNotEqual(base, mod._preview_signature(node3))
        # 未变 → 签名稳定
        same = self._make_zone_empty()
        node4 = self._make_preview_node(mod, tree_name="Tree", node_name="Drag",
                                        target=target, zone_empties=[same])
        ctx.selected_objects = [same]
        self.assertEqual(base, mod._preview_signature(node4))

    def test_signature_sensitive_to_target_move(self):
        mod = self.mod
        empty = self._make_zone_empty()
        mod.bpy.context.selected_objects = [empty]  # t12：选中才进签名
        m = np.eye(4)
        m[2, 3] = 5.0
        node = self._make_preview_node(
            mod, target=types.SimpleNamespace(name="Mesh", matrix_world=m,
                                              data=types.SimpleNamespace(vertices=range(100))),
            zone_empties=[empty])
        base = mod._preview_signature(node)
        m2 = np.eye(4)
        m2[2, 3] = 6.0
        node2 = self._make_preview_node(
            mod, target=types.SimpleNamespace(name="Mesh", matrix_world=m2,
                                              data=types.SimpleNamespace(vertices=range(100))),
            zone_empties=[empty])
        self.assertNotEqual(base, mod._preview_signature(node2))

    def test_collection_preview_recursively_collects_meshes_and_takes_priority(self):
        mod = self.mod
        mesh_a = types.SimpleNamespace(type="MESH", name="A", name_full="A", matrix_world=np.eye(4),
                                       data=types.SimpleNamespace(vertices=range(3)))
        mesh_b = types.SimpleNamespace(type="MESH", name="B", name_full="B", matrix_world=np.eye(4),
                                       data=types.SimpleNamespace(vertices=range(5)))
        light = types.SimpleNamespace(type="LIGHT", name="Light", name_full="Light")
        collection = types.SimpleNamespace(name="PreviewSet", all_objects=[mesh_b, light, mesh_a, mesh_a])
        fallback = types.SimpleNamespace(type="MESH", name="Fallback", matrix_world=np.eye(4),
                                         data=types.SimpleNamespace(vertices=range(1)))
        node = self._make_preview_node(mod, target=fallback, collection=collection)
        self.assertEqual([obj.name for obj in mod._preview_targets(node)], ["A", "B"])
        signature = mod._preview_signature(node)
        self.assertIn("PreviewSet", signature)
        self.assertNotIn("Fallback", str(signature))

    def test_collection_membership_changes_preview_signature(self):
        mod = self.mod
        mesh_a = types.SimpleNamespace(type="MESH", name="A", name_full="A", matrix_world=np.eye(4),
                                       data=types.SimpleNamespace(vertices=range(3)))
        mesh_b = types.SimpleNamespace(type="MESH", name="B", name_full="B", matrix_world=np.eye(4),
                                       data=types.SimpleNamespace(vertices=range(5)))
        collection = types.SimpleNamespace(name="PreviewSet", all_objects=[mesh_a])
        # t12：目标进签名的前提是节点有选中的区域空物体（否则预览不渲染、
        # 签名最小化，目标/集合变化零 churn）
        empty = self._make_zone_empty()
        node = self._make_preview_node(mod, collection=collection, zone_empties=[empty])
        mod.bpy.context.selected_objects = [empty]
        before = mod._preview_signature(node)
        collection.all_objects.append(mesh_b)
        self.assertNotEqual(before, mod._preview_signature(node))

    def test_preview_tick_debounces_repeated_signature_changes(self):
        mod = self.mod
        node = self._make_preview_node(mod)
        signatures = iter(("initial", "change-1", "change-2", "change-2"))
        rebuilds = []
        clock = iter((0.0, 0.2, 0.4, 1.2))
        original = (
            mod._collect_preview_nodes,
            mod._ensure_preview_handler,
            mod._preview_signature,
            mod._rebuild_preview_batches,
            mod._preview_now,
        )
        mod._preview_sig_cache = None
        mod._preview_pending_signature = None
        mod._preview_pending_since = None
        try:
            mod._collect_preview_nodes = lambda: [node]
            mod._ensure_preview_handler = lambda: None
            mod._preview_signature = lambda _node: next(signatures)
            mod._rebuild_preview_batches = lambda nodes: rebuilds.append(list(nodes))
            mod._preview_now = lambda: next(clock)

            mod._preview_tick()  # 首次开启立即建立预览
            mod._preview_tick()  # 连续新增区域，只记录待处理签名
            mod._preview_tick()  # 签名继续变化，重新开始防抖计时
            mod._preview_tick()  # 稳定超过防抖窗口，只重建一次
        finally:
            (mod._collect_preview_nodes,
             mod._ensure_preview_handler,
             mod._preview_signature,
             mod._rebuild_preview_batches,
             mod._preview_now) = original
            mod._preview_sig_cache = None
            mod._preview_pending_signature = None
            mod._preview_pending_since = None

        self.assertEqual(len(rebuilds), 2)

    def test_preview_target_field_uses_scene_space(self):
        """预览基于 Blender 当前场景坐标（所见即所得），不做任何导出期变换：
        非镜像 X 镜像补偿、参考物体逆、导出空间矩阵都只属于导出烘焙。
        否则球会被翻到另一侧，沿表面扩散到非当前物体。"""
        mod = self.mod
        node = _make_node(mod)
        object.__setattr__(node, "surface_propagate", True)
        object.__setattr__(node, "mask_plateau", 0.0)
        # 球心 X+1（半径 1）在 A 上；B 在 X-0.5，距球心 1.5 > 1，场景中不相交
        empty = self._make_zone_empty(loc=(1.0, 0.0, 0.0), scale=1.0)
        zones = [(empty, empty.ssmt_drag_zone)]
        verts_a = np.array([[0.9, 0.0, 0.0], [1.0, 0.0, 0.0], [1.1, 0.0, 0.0]], dtype=np.float64)
        verts_b = np.array([[-0.5, 0.0, 0.0], [-0.6, 0.0, 0.0], [-0.5, 0.1, 0.0]], dtype=np.float64)
        tris = np.array([[0, 1, 2]], dtype=np.int64)
        field_a = mod._preview_target_field(node, verts_a, tris, zones, 0.0)
        field_b = mod._preview_target_field(node, verts_b, tris, zones, 0.0)
        self.assertGreater(float(field_a.max()), 0.3)      # A 有沿表面权重
        np.testing.assert_array_equal(field_b, np.zeros(3))  # B 不被接触/扩散

        # 对照：若错误地在预览中只对球施加 X 镜像（顶点保持场景坐标），
        # 球会翻到 X- 一侧错误接触 B —— 正是本 bug 的机制
        ball_mirrored = np.diag([-1.0, 1.0, 1.0, 1.0]) @ np.asarray(empty.matrix_world, dtype=np.float64)
        d_bug = node._zone_distances(verts_b, ball_mirrored, mod.gb_core.edges_from_triangles(tris))
        f_bug = node._shape_field(d_bug, 1.0, 4.6, 0.0)
        self.assertGreater(float(f_bug.max()), 0.0)

    def test_merged_mesh_welds_shared_seam_between_objects(self):
        """集合预览：共享接缝的两个网格必须被焊接为同一个连续表面。
        球命中 A 后应沿表面穿过接缝传播到 B；结果必须与手工焊成单个
        网格计算的场完全一致（即集合 = 一个物体）。"""
        mod = self.mod
        node = _make_node(mod)
        object.__setattr__(node, "surface_propagate", True)
        object.__setattr__(node, "mask_plateau", 0.0)
        # 球心 (0.15,0,0) 半径 0.25：A 沿 x（接缝在 x=0.2），B 从接缝沿 y 拐弯
        empty = self._make_zone_empty(loc=(0.15, 0.0, 0.0), scale=0.25)
        zones = [(empty, empty.ssmt_drag_zone)]
        verts_a = np.array([[0.1, 0.0, 0.0], [0.15, 0.0, 0.0], [0.2, 0.0, 0.0]], dtype=np.float64)
        tris_a = np.array([[0, 1, 2]], dtype=np.int64)
        verts_b = np.array([[0.2, 0.0, 0.0], [0.2, 0.1, 0.0], [0.2, 0.2, 0.0]], dtype=np.float64)
        tris_b = np.array([[0, 1, 2]], dtype=np.int64)

        fields = mod._preview_merged_mesh(
            node,
            [(verts_a, tris_a), (verts_b, tris_b)],
            zones,
            0.0,
        )
        self.assertEqual(len(fields), 2)

        # 手工焊接成单个网格：B[0] 与 A[2] 共享接缝顶点
        welded_verts = np.concatenate([verts_a, verts_b[1:]], axis=0)
        welded_tris = np.array([[0, 1, 2], [2, 3, 4]], dtype=np.int64)
        ref = mod._preview_target_field(node, welded_verts, welded_tris, zones, 0.0)

        np.testing.assert_allclose(fields[0], ref[:3], atol=1e-12)
        # B 的接缝顶点与 A 末端同节点 → 权重一致；B 其余顶点按接缝连续传播
        np.testing.assert_allclose(fields[1], np.concatenate([[ref[2]], ref[3:]]), atol=1e-12)

    def test_merged_mesh_keeps_disconnected_parts_zero(self):
        """集合预览：不共享接缝（互不相连）的网格不能被沿表面传播到，
        只有真正被球接触的物体有权重。"""
        mod = self.mod
        node = _make_node(mod)
        object.__setattr__(node, "surface_propagate", True)
        object.__setattr__(node, "mask_plateau", 0.0)
        empty = self._make_zone_empty(loc=(0.15, 0.0, 0.0), scale=0.25)
        zones = [(empty, empty.ssmt_drag_zone)]
        verts_a = np.array([[0.1, 0.0, 0.0], [0.15, 0.0, 0.0], [0.2, 0.0, 0.0]], dtype=np.float64)
        tris_a = np.array([[0, 1, 2]], dtype=np.int64)
        # B 平移 z=3，与 A 无共享顶点 → 不连通
        verts_b = np.array([[0.1, 0.0, 3.0], [0.15, 0.0, 3.0], [0.2, 0.0, 3.0]], dtype=np.float64)
        tris_b = np.array([[0, 1, 2]], dtype=np.int64)

        fields = mod._preview_merged_mesh(
            node,
            [(verts_a, tris_a), (verts_b, tris_b)],
            zones,
            0.0,
        )
        self.assertGreater(float(fields[0].max()), 0.0)
        np.testing.assert_array_equal(fields[1], np.zeros(3))

    def test_topology_cache_reuses_unchanged_mesh(self):
        """焊接/边拓扑按网格数据缓存：网格未变时返回同一拓扑对象，
        避免每次预览重建重算 np.unique 焊接与邻接表。"""
        mod = self.mod
        verts = np.array([[0.1, 0.0, 0.0], [0.15, 0.0, 0.0], [0.2, 0.0, 0.0]], dtype=np.float64)
        tris = np.array([[0, 1, 2]], dtype=np.int64)
        topo1 = mod._preview_cached_topology(verts, tris)
        topo2 = mod._preview_cached_topology(verts.copy(), tris.copy())
        self.assertIs(topo1, topo2)
        self.assertIsNotNone(topo1["adjacency"])
        # 顶点变化 → 不同拓扑
        moved = verts + 1e-2
        topo3 = mod._preview_cached_topology(moved, tris)
        self.assertIsNot(topo1, topo3)
        # 集合焊接模式同样命中缓存
        topo_m1 = mod._preview_cached_topology(verts, tris, weld_tol=1e-5)
        topo_m2 = mod._preview_cached_topology(verts.copy(), tris.copy(), weld_tol=1e-5)
        self.assertIs(topo_m1, topo_m2)

    def test_uniform_scale_zone_fast_path_matches_slow_path(self):
        """均匀缩放球预览快速路径（复用世界邻接表）必须与逐球局部构建一致。"""
        mod = self.mod
        node = _make_node(mod)
        object.__setattr__(node, "surface_propagate", True)
        verts = np.array(
            [[0.0, 0.0, 0.0], [0.1, 0.0, 0.0], [0.2, 0.0, 0.0], [0.3, 0.0, 0.0]],
            dtype=np.float64,
        )
        tris = np.array([[0, 1, 2], [1, 2, 3]], dtype=np.int64)
        edges = mod.gb_core.edges_from_triangles(tris)
        topo = mod._preview_cached_topology(verts, tris)
        # 旋转 + 平移 + 等比缩放的球
        theta = 0.5
        m = np.eye(4)
        m[:3, :3] = np.array([
            [np.cos(theta), -np.sin(theta), 0.0],
            [np.sin(theta), np.cos(theta), 0.0],
            [0.0, 0.0, 1.0],
        ]) * 0.5
        m[:3, 3] = (0.12, 0.02, 0.0)
        d_fast = mod._preview_zone_distances(
            node, verts, m, topo["adjacency"], topo["edge_verts"]
        )
        d_slow = node._zone_distances(verts, m, edges)
        np.testing.assert_allclose(d_fast, d_slow, atol=1e-12)

    def test_zone_field_cache_reuses_unchanged_ball(self):
        """逐球权重场缓存：同一拓扑 + 同一球参数再次求场直接命中，
        只重算变化的球（大量区域下拖动单球不再全量 Dijkstra）。"""
        mod = self.mod
        node = _make_node(mod)
        object.__setattr__(node, "mask_plateau", 0.0)
        mod._PREVIEW_ZONE_FIELD_CACHE.clear()
        verts = np.array([[0.0, 0.0, 0.0], [0.1, 0.0, 0.0], [0.2, 0.0, 0.0]], dtype=np.float64)
        tris = np.array([[0, 1, 2]], dtype=np.int64)
        topo_key = mod._preview_topology_key(verts, tris, None)
        topo = mod._preview_cached_topology(verts, tris, key=topo_key)
        empty = self._make_zone_empty(loc=(0.05, 0.0, 0.0), scale=0.25)
        s = empty.ssmt_drag_zone
        f1 = mod._preview_zone_field(node, topo, topo_key, empty, s, 0.0)
        self.assertGreater(float(f1.max()), 0.0)
        # 同参数再次调用 → 命中缓存，不再调用 _preview_zone_distances
        calls = []
        original = mod._preview_zone_distances
        mod._preview_zone_distances = lambda *a, **k: calls.append(1) or original(*a, **k)
        try:
            f2 = mod._preview_zone_field(node, topo, topo_key, empty, s, 0.0)
        finally:
            mod._preview_zone_distances = original
        np.testing.assert_array_equal(f1, f2)
        self.assertEqual(len(calls), 0)
        # 移动球 → 重新计算
        moved = self._make_zone_empty(loc=(0.5, 0.0, 0.0), scale=0.25)
        f3 = mod._preview_zone_field(node, topo, topo_key, moved, moved.ssmt_drag_zone, 0.0)
        self.assertFalse(np.array_equal(f1, f3))

    def test_mesh_data_cache_reuses_unchanged_target(self):
        """网格数据缓存：目标矩阵/顶点/面数未变时复用 verts/tri，
        不再 foreach_get / calc_loop_triangles（大量区域下预览重建的主要开销）。"""
        mod = self.mod
        node = _make_node(mod)
        mod._PREVIEW_MESH_CACHE.clear()

        class FakeVerts:
            def __init__(self, n):
                self._n = n

            def __len__(self):
                return self._n

            def foreach_get(self, attr, dest):
                dest[:] = [0.0, 0.0, 0.0] * (len(dest) // 3)

        class FakeMesh:
            vertices = FakeVerts(3)
            polygons = [1]
            class _LoopTriangles:
                def __len__(self):
                    return 1

                def foreach_get(self, attr, dest):
                    dest[:] = [0, 1, 2]

            loop_triangles = _LoopTriangles()

            def calc_loop_triangles(self):
                pass

        target = types.SimpleNamespace(
            name="Mesh", matrix_world=np.eye(4), data=FakeMesh()
        )
        object.__setattr__(node, "id_data", types.SimpleNamespace(name="Tree"))
        object.__setattr__(node, "name", "Drag")
        r1 = mod._preview_mesh_data(node, target)
        self.assertIsNotNone(r1)
        # 未变 → 命中缓存（calc_loop_triangles 不再调用）
        calls = []
        orig = FakeMesh.calc_loop_triangles
        FakeMesh.calc_loop_triangles = lambda self: calls.append(1)
        try:
            r2 = mod._preview_mesh_data(node, target)
        finally:
            FakeMesh.calc_loop_triangles = orig
        self.assertEqual(len(calls), 0)
        self.assertIs(r1[0], r2[0])
        self.assertIs(r1[1], r2[1])
        # 矩阵变化 → 重新读取
        target.matrix_world = np.eye(4) * 2.0
        target.matrix_world[3, 3] = 1.0
        r3 = mod._preview_mesh_data(node, target)
        self.assertIsNot(r1[0], r3[0])


class DragNodePreviewSelectionTests(unittest.TestCase):
    """权重预览选中过滤（t12 需求变更）：只预览选中的区域空物体——
    多选 = 并集（active 无特权）；未选中任何区域空物体 = 不显示预览。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()

    def setUp(self):
        self._orig_context = getattr(self.mod.bpy, "context", None)
        self.mod.bpy.context = types.SimpleNamespace(
            active_object=None, selected_objects=())

    def tearDown(self):
        self.mod.bpy.context = self._orig_context

    def _zone_empty(self, name="SSMT_DragZone_0", loc=(0.0, 0.0, 0.0),
                    enabled=True):
        m = np.eye(4)
        if loc is not None:
            m[:3, 3] = loc
        settings = types.SimpleNamespace(
            enabled=enabled, brush_strength=1.0, brush_falloff_k=4.6,
            propagate=True, include_objects=[])
        return types.SimpleNamespace(name=name, matrix_world=m,
                                     ssmt_drag_zone=settings)

    def _make_preview_node(self, target, empties, tree="Tree", name="Drag"):
        node = _make_node(self.mod)
        object.__setattr__(node, "preview_weights", True)
        object.__setattr__(node, "preview_target", target)
        object.__setattr__(node, "preview_collection", None)
        object.__setattr__(node, "id_data", types.SimpleNamespace(name=tree))
        object.__setattr__(node, "name", name)
        object.__setattr__(node, "zone_objects",
                           [types.SimpleNamespace(zone_object=e) for e in empties])
        return node

    def test_selected_zones_union_active_has_no_privilege(self):
        """t12 多选并集：多个区内空物体被选 → 全部返回（按 zone_objects 顺序），
        active 无特权（只是 selected 的普通成员）。"""
        mod = self.mod
        a = self._zone_empty(name="A")
        b = self._zone_empty(name="B")
        ctx = mod.bpy.context
        ctx.active_object = a
        ctx.selected_objects = [a, b]
        node = self._make_preview_node(
            types.SimpleNamespace(name="Mesh", matrix_world=np.eye(4),
                                  data=types.SimpleNamespace(vertices=range(3))),
            [a, b])
        sel = mod._preview_selected_zones(node)
        self.assertEqual([z[0].name for z in sel], ["A", "B"])
        zones = mod._preview_node_zones(node)
        self.assertEqual([z[0].name for z in zones], ["A", "B"])
        # active 换成 b 不改变并集内容
        ctx.active_object = b
        self.assertEqual([z[0].name for z in mod._preview_node_zones(node)], ["A", "B"])

    def test_sole_selected_zone_when_active_is_other(self):
        """active 是他物、selected 与区内交集恰好一个 → 只预览它。"""
        mod = self.mod
        a = self._zone_empty(name="A")
        b = self._zone_empty(name="B")
        other = types.SimpleNamespace(name="Mesh")
        ctx = mod.bpy.context
        ctx.active_object = other
        ctx.selected_objects = [other, b]
        node = self._make_preview_node(
            types.SimpleNamespace(name="Mesh", matrix_world=np.eye(4),
                                  data=types.SimpleNamespace(vertices=range(3))),
            [a, b])
        sel = mod._preview_selected_zones(node)
        self.assertEqual(len(sel), 1)
        self.assertIs(sel[0][0], b)

    def test_no_selection_yields_no_preview(self):
        """t12 需求变更（推翻 t1『显示全部』）：未选中任何区域空物体 →
        返回空列表 → rebuild 跳过该节点 → 不显示任何预览。"""
        mod = self.mod
        a = self._zone_empty(name="A")
        b = self._zone_empty(name="B")
        ctx = mod.bpy.context
        ctx.active_object = None
        ctx.selected_objects = []
        node = self._make_preview_node(
            types.SimpleNamespace(name="Mesh", matrix_world=np.eye(4),
                                  data=types.SimpleNamespace(vertices=range(3))),
            [a, b])
        self.assertEqual(mod._preview_selected_zones(node), [])
        self.assertEqual(mod._preview_node_zones(node), [])
        # 选中的都不是本节点区域空物体（无关选择）→ 同样无预览、零重算
        ctx.selected_objects = [types.SimpleNamespace(name="Unrelated")]
        self.assertEqual(mod._preview_node_zones(node), [])

    def test_multiple_selected_zones_all_preview(self):
        """t12 多选：多个区内空物体被选（无论 active 是否区内）→ 并集全部预览。"""
        mod = self.mod
        a = self._zone_empty(name="A")
        b = self._zone_empty(name="B")
        other = types.SimpleNamespace(name="Mesh")
        ctx = mod.bpy.context
        ctx.active_object = other
        ctx.selected_objects = [a, b]
        node = self._make_preview_node(
            types.SimpleNamespace(name="Mesh", matrix_world=np.eye(4),
                                  data=types.SimpleNamespace(vertices=range(3))),
            [a, b])
        self.assertEqual([z[0].name for z in mod._preview_selected_zones(node)],
                         ["A", "B"])

    def test_disabled_selected_zone_yields_no_zones(self):
        """选中区域 disabled → 预览不渲染（返回空列表）。"""
        mod = self.mod
        a = self._zone_empty(name="A", enabled=False)
        ctx = mod.bpy.context
        ctx.active_object = a
        ctx.selected_objects = [a]
        node = self._make_preview_node(
            types.SimpleNamespace(name="Mesh", matrix_world=np.eye(4),
                                  data=types.SimpleNamespace(vertices=range(3))),
            [a])
        self.assertEqual(mod._preview_node_zones(node), [])

    def test_selected_zone_field_only_that_zone(self):
        """多区域时只选中 A → 场只由 A 计算；远处顶点（只属于 B）权重为 0；
        t12 多选：A、B 都选中 → 场 = 两者并集（远处顶点恢复权重）。"""
        mod = self.mod
        node = _make_node(mod)
        object.__setattr__(node, "mask_plateau", 0.0)
        object.__setattr__(node, "surface_propagate", True)
        a = self._zone_empty(name="A", loc=(1.0, 0.0, 0.0))
        b = self._zone_empty(name="B", loc=(10.0, 0.0, 0.0))
        ctx = mod.bpy.context
        ctx.active_object = a
        ctx.selected_objects = [a]  # 只选中 A（t12：不再以 active 缩小多选）
        verts = np.array([[0.9, 0.0, 0.0], [1.0, 0.0, 0.0], [1.1, 0.0, 0.0],
                          [9.9, 0.0, 0.0]], dtype=np.float64)
        tris = np.array([[0, 1, 2]], dtype=np.int64)
        pnode = self._make_preview_node(
            types.SimpleNamespace(name="Mesh", matrix_world=np.eye(4),
                                  data=types.SimpleNamespace(vertices=range(3))),
            [a, b])
        node_zones = mod._preview_node_zones(pnode)
        self.assertEqual(len(node_zones), 1)
        self.assertIs(node_zones[0][0], a)
        field_sel = mod._preview_target_field(node, verts, tris, node_zones, 0.0)
        field_a = mod._preview_target_field(
            node, verts, tris, [(a, a.ssmt_drag_zone)], 0.0)
        field_ab = mod._preview_target_field(
            node, verts, tris, [(a, a.ssmt_drag_zone), (b, b.ssmt_drag_zone)], 0.0)
        np.testing.assert_array_equal(field_sel, field_a)
        self.assertEqual(float(field_sel[3]), 0.0)      # 远处顶点只属于 B
        self.assertGreater(float(field_ab[3]), 0.0)     # 全部区域时 B 参与
        # t12 多选并集：A、B 都选中 → 场等于两者合并
        ctx.selected_objects = [a, b]
        node_zones_ab = mod._preview_node_zones(pnode)
        self.assertEqual(len(node_zones_ab), 2)
        field_sel_ab = mod._preview_target_field(node, verts, tris, node_zones_ab, 0.0)
        np.testing.assert_array_equal(field_sel_ab, field_ab)

    def test_signature_changes_on_selection_switch(self):
        """选中状态进入签名：A→B、A→无选择 都触发重建。"""
        mod = self.mod
        target = types.SimpleNamespace(name="Mesh", matrix_world=np.eye(4),
                                       data=types.SimpleNamespace(vertices=range(3)))
        a = self._zone_empty(name="A")
        b = self._zone_empty(name="B")
        ctx = mod.bpy.context
        node = self._make_preview_node(target, [a, b])
        ctx.active_object = a
        ctx.selected_objects = [a]
        sig_a = mod._preview_signature(node)
        ctx.active_object = b
        ctx.selected_objects = [b]
        self.assertNotEqual(sig_a, mod._preview_signature(node))
        ctx.active_object = None
        ctx.selected_objects = []
        self.assertNotEqual(sig_a, mod._preview_signature(node))

    def test_signature_ignores_unselected_zone_movement(self):
        """选中 A 时移动未选中的 B → 签名不变（零 churn）；
        移动选中的 A → 签名变化。t12：多选下 A、B 都被选则两者都进签名。"""
        mod = self.mod
        target = types.SimpleNamespace(name="Mesh", matrix_world=np.eye(4),
                                       data=types.SimpleNamespace(vertices=range(3)))
        a = self._zone_empty(name="A")
        b = self._zone_empty(name="B")
        ctx = mod.bpy.context
        ctx.active_object = a
        ctx.selected_objects = [a]  # 只选中 A（B 的移动不进签名）
        node = self._make_preview_node(target, [a, b])
        sig_before = mod._preview_signature(node)
        m = np.eye(4)
        m[0, 3] = 5.0
        b.matrix_world = m
        self.assertEqual(sig_before, mod._preview_signature(node))
        a.matrix_world = m
        self.assertNotEqual(sig_before, mod._preview_signature(node))

    def test_signature_deform_and_pose_sensitivity(self):
        """形变签名进入签名：形态键值变化、骨骼姿态变化触发重建；
        无形态键/无骨骼 → deform 分量恒定 None。"""
        mod = self.mod
        plain = types.SimpleNamespace(name="M", data=types.SimpleNamespace())
        self.assertIsNone(mod._preview_deform_signature(plain))
        kb = [types.SimpleNamespace(name="Key", value=0.0, mute=False)]
        sk = types.SimpleNamespace(key_blocks=kb)
        target = types.SimpleNamespace(
            name="Mesh", matrix_world=np.eye(4),
            data=types.SimpleNamespace(vertices=range(3), shape_keys=sk))
        a = self._zone_empty(name="A")
        ctx = mod.bpy.context
        ctx.active_object = a
        ctx.selected_objects = [a]
        node = self._make_preview_node(target, [a])
        sig_before = mod._preview_signature(node)
        kb[0].value = 0.5
        self.assertNotEqual(sig_before, mod._preview_signature(node))
        # 骨骼姿态变化（无形态键）→ 签名变化
        bone = types.SimpleNamespace(name="Bone", matrix=np.eye(4))
        pose = types.SimpleNamespace(bones=[bone])
        arm = types.SimpleNamespace(name="Arm",
                                    data=types.SimpleNamespace(pose_position="POSE"),
                                    pose=pose)
        arm_mod = types.SimpleNamespace(type="ARMATURE", object=arm)
        target2 = types.SimpleNamespace(
            name="Mesh2", matrix_world=np.eye(4),
            data=types.SimpleNamespace(vertices=range(3)),
            modifiers=[arm_mod])
        node2 = self._make_preview_node(target2, [a], name="Drag2")
        sig2_before = mod._preview_signature(node2)
        rotated = np.array([[0.0, -1.0, 0.0, 0.0],
                            [1.0, 0.0, 0.0, 0.0],
                            [0.0, 0.0, 1.0, 0.0],
                            [0.0, 0.0, 0.0, 1.0]], dtype=np.float64)
        bone.matrix = rotated
        # 姿态摘要每代缓存一次：同一代内返回旧摘要（防每 tick 重复全量遍历），
        # 模拟下一 tick（代号推进）后检出新姿态 → 签名变化
        self.assertEqual(sig2_before, mod._preview_signature(node2))
        mod._PREVIEW_DIGEST_GENERATION += 1
        self.assertNotEqual(sig2_before, mod._preview_signature(node2))


class DragNodePreviewDeformTests(unittest.TestCase):
    """形变感知网格读取：evaluated mesh 路径 + 形变签名缓存失效 + 失败回退。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()

    def setUp(self):
        self._orig_context = getattr(self.mod.bpy, "context", None)
        self.mod.bpy.context = types.SimpleNamespace(
            active_object=None, selected_objects=())

    def tearDown(self):
        self.mod.bpy.context = self._orig_context

    def _make_fake_mesh(self, verts_local, tris):
        """构造可 foreach_get 的假网格（模拟 bpy Mesh）。"""
        verts_arr = np.asarray(verts_local, dtype=np.float64)
        tris_arr = np.asarray(tris, dtype=np.int64)

        class FakeVerts:
            def __len__(self):
                return len(verts_arr)

            def foreach_get(self, attr, dest):
                dest[:] = verts_arr.reshape(-1)[:len(dest)]

        class FakeTriangles:
            def __len__(self):
                return len(tris_arr)

            def foreach_get(self, attr, dest):
                dest[:] = tris_arr.reshape(-1)[:len(dest)]

        class FakeMesh:
            vertices = FakeVerts()
            polygons = [1] * len(tris_arr)
            loop_triangles = FakeTriangles()

            def calc_loop_triangles(self):
                pass

        return FakeMesh()

    def _make_base_target(self, name="Mesh", sk_value=0.0):
        mesh = self._make_fake_mesh(
            [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0]], [[0, 1, 2]])
        kb = [types.SimpleNamespace(name="Key", value=sk_value, mute=False)]
        mesh.shape_keys = types.SimpleNamespace(key_blocks=kb)
        return types.SimpleNamespace(name=name, matrix_world=np.eye(4), data=mesh)

    def _attach_eval_geometry(self, target, eval_verts, tris):
        """给目标挂 evaluated_get（真实 API：object.evaluated_get(depsgraph)），
        返回产出 eval_verts 的假评估物体；返回调用计数列表。"""
        eval_mesh = self._make_fake_mesh(eval_verts, tris)
        calls = []

        class FakeEvalObj:
            def to_mesh(self):
                return eval_mesh

            def to_mesh_clear(self):
                pass

        def evaluated_get(depsgraph):
            calls.append(depsgraph)
            return FakeEvalObj()

        target.evaluated_get = evaluated_get
        return calls

    def _node(self):
        node = _make_node(self.mod)
        object.__setattr__(node, "id_data", types.SimpleNamespace(name="Tree"))
        object.__setattr__(node, "name", "Drag")
        return node

    def test_evaluated_mesh_path_uses_deformed_verts(self):
        """带 shape key 的目标 + 可用 depsgraph → 预览顶点取自求值网格（形变后），
        缓存命中后不再走 evaluated_get。"""
        mod = self.mod
        mod._PREVIEW_MESH_CACHE.clear()
        target = self._make_base_target(sk_value=0.5)
        base = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0]],
                        dtype=np.float64)
        deformed = base + np.array([[0.0, 0.0, 0.3]] * 3)
        eval_calls = self._attach_eval_geometry(target, deformed, [[0, 1, 2]])
        node = self._node()
        result = mod._preview_mesh_data(node, target, object())
        self.assertIsNotNone(result)
        verts_world, tri, topo_key = result
        # 世界顶点应为形变后位置（z 抬升 0.3），而非基础网格 z=0
        self.assertGreater(float(verts_world[:, 2].max()), 0.29)
        np.testing.assert_allclose(verts_world, deformed, atol=1e-12)
        self.assertEqual(len(eval_calls), 1)
        # 同 matrix/vcount/pcount/deform_sig → 缓存命中，evaluated_get 不再被调
        r2 = mod._preview_mesh_data(node, target, object())
        np.testing.assert_array_equal(r2[0], verts_world)
        self.assertEqual(len(eval_calls), 1)

    def test_deform_signature_change_invalidates_mesh_cache(self):
        """matrix/vcount/pcount 未变但形态键值变化 → 重新读求值网格。"""
        mod = self.mod
        mod._PREVIEW_MESH_CACHE.clear()
        target = self._make_base_target(sk_value=0.5)
        base = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0]],
                        dtype=np.float64)
        eval_calls = self._attach_eval_geometry(
            target, base + np.array([[0.0, 0.0, 0.3]] * 3), [[0, 1, 2]])
        node = self._node()
        r1 = mod._preview_mesh_data(node, target, object())
        self.assertEqual(len(eval_calls), 1)
        target.data.shape_keys.key_blocks[0].value = 0.7
        r2 = mod._preview_mesh_data(node, target, object())
        self.assertEqual(len(eval_calls), 2)
        self.assertIsNot(r1[0], r2[0])

    def test_evaluated_get_failure_falls_back_to_base(self):
        """evaluated_get 抛异常 → 安全回退基础网格，不抛异常。"""
        mod = self.mod
        mod._PREVIEW_MESH_CACHE.clear()
        target = self._make_base_target(sk_value=0.5)

        def boom(depsgraph):
            raise RuntimeError("no depsgraph")

        target.evaluated_get = boom
        node = self._node()
        result = mod._preview_mesh_data(node, target, object())
        self.assertIsNotNone(result)
        np.testing.assert_allclose(result[0][:, 2], np.zeros(3), atol=1e-12)

    def test_no_depsgraph_or_no_deform_stays_static(self):
        """无 depsgraph → 基础网格；目标无形态键/无骨骼 → 即使有 depsgraph
        也不触发求值（deform_sig=None 静态路径）。"""
        mod = self.mod
        mod._PREVIEW_MESH_CACHE.clear()
        target = self._make_base_target(sk_value=0.5)
        eval_calls = self._attach_eval_geometry(
            target, [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0]],
            [[0, 1, 2]])
        node = self._node()
        # 无 depsgraph → evaluated_get 不被调用，基础网格
        r = mod._preview_mesh_data(node, target, None)
        self.assertIsNotNone(r)
        np.testing.assert_allclose(r[0][:, 2], np.zeros(3), atol=1e-12)
        self.assertEqual(len(eval_calls), 0)
        # 有 depsgraph 但目标无形态键/无骨骼 → 不改走求值
        plain_mesh = self._make_fake_mesh([[0.0, 0.0, 0.0]], [[0, 0, 0]])
        plain = types.SimpleNamespace(name="Plain", matrix_world=np.eye(4),
                                      data=plain_mesh)
        calls = []

        def spy(depsgraph):
            calls.append(1)
            raise RuntimeError("should not be called")

        plain.evaluated_get = spy
        r2 = mod._preview_mesh_data(node, plain, object())
        self.assertIsNotNone(r2)
        self.assertEqual(len(calls), 0)

    def test_deform_signature_helper_detail(self):
        """形变签名内容：形态键、修改器类型指纹、骨骼姿态、约束目标矩阵；
        无形态键且无任何修改器 → None（静态路径）。"""
        mod = self.mod
        # 形态键
        kb = [types.SimpleNamespace(name="K1", value=0.25, mute=False)]
        sk = types.SimpleNamespace(key_blocks=kb)
        sk_target = types.SimpleNamespace(
            name="M", data=types.SimpleNamespace(vertices=range(3), shape_keys=sk))
        sig_sk = mod._preview_deform_signature(sk_target)
        self.assertIsNotNone(sig_sk)
        self.assertIn("K1", str(sig_sk))
        # 骨骼姿态 + IK 约束目标
        ik = types.SimpleNamespace(name="IK", matrix_world=np.eye(4))
        bone = types.SimpleNamespace(name="B1", matrix=np.eye(4),
                                     constraints=[types.SimpleNamespace(target=ik)])
        pose = types.SimpleNamespace(bones=[bone])
        arm = types.SimpleNamespace(name="Arm",
                                    data=types.SimpleNamespace(pose_position="POSE"),
                                    pose=pose)
        arm_mod = types.SimpleNamespace(type="ARMATURE", object=arm)
        skinned = types.SimpleNamespace(
            name="S", data=types.SimpleNamespace(vertices=range(3)),
            modifiers=[arm_mod])
        sig_arm = mod._preview_deform_signature(skinned)
        self.assertIsNotNone(sig_arm)
        self.assertIn("Arm", str(sig_arm))
        # IK 约束目标矩阵变化（下一 tick，代号推进）→ 摘要变化（覆盖 IK 目标移动）
        mod._PREVIEW_DIGEST_GENERATION += 1
        ik.matrix_world = np.eye(4) * 2.0
        ik.matrix_world[3, 3] = 1.0
        self.assertNotEqual(sig_arm, mod._preview_deform_signature(skinned))
        # 修改器类型指纹：非 ARMATURE 修改器（SUBSURF）也计入形变源
        plain_mod = types.SimpleNamespace(type="SUBSURF", object=None)
        t = types.SimpleNamespace(name="T", data=types.SimpleNamespace(),
                                  modifiers=[plain_mod])
        sig_mod = mod._preview_deform_signature(t)
        self.assertIsNotNone(sig_mod)
        self.assertIn(("MOD", ("SUBSURF",)), sig_mod)
        # 修改器增删/类型变化 → 指纹变化
        self.assertNotEqual(
            sig_mod,
            mod._preview_deform_signature(
                types.SimpleNamespace(name="T", data=types.SimpleNamespace(),
                                      modifiers=[])))
        self.assertNotEqual(
            sig_mod,
            mod._preview_deform_signature(
                types.SimpleNamespace(name="T", data=types.SimpleNamespace(),
                                      modifiers=[types.SimpleNamespace(
                                          type="MIRROR", object=None)])))
        # 无形态键且无任何修改器 → None（静态路径，性能）
        self.assertIsNone(mod._preview_deform_signature(
            types.SimpleNamespace(name="T", data=types.SimpleNamespace())))

    def test_volume_mode_field_invalidates_on_geometry_change(self):
        """propagate=False（体积球）回归：形变前后不同几何（同顶点数）→
        场必须不同——vol 键并入内容哈希，防逐球场缓存误命中旧几何。"""
        mod = self.mod
        mod._PREVIEW_ZONE_FIELD_CACHE.clear()
        node = _make_node(mod)
        object.__setattr__(node, "mask_plateau", 0.0)
        settings = types.SimpleNamespace(
            enabled=True, brush_strength=1.0, brush_falloff_k=4.6,
            propagate=False, include_objects=[])
        empty = types.SimpleNamespace(name="z", matrix_world=np.eye(4),
                                      ssmt_drag_zone=settings)
        zones = [(empty, settings)]
        tris = np.array([[0, 1, 2]], dtype=np.int64)
        # 形变前：顶点在球（原点，半径 1）内
        verts_a = np.array([[0.0, 0.0, 0.0], [0.5, 0.0, 0.0], [0.0, 0.5, 0.0]],
                           dtype=np.float64)
        # 形变后：同顶点数，几何整体远离球（z+3，d>=1 硬截止为 0）
        verts_b = verts_a + np.array([[0.0, 0.0, 3.0]] * 3)
        field_a = mod._preview_target_field(node, verts_a, tris, zones, 0.0)
        field_b = mod._preview_target_field(node, verts_b, tris, zones, 0.0)
        self.assertGreater(float(field_a.max()), 0.0)
        np.testing.assert_array_equal(field_b, np.zeros(3))
        self.assertFalse(np.array_equal(field_a, field_b))


class DragNodePreviewPerfTests(unittest.TestCase):
    """性能回归：tick 签名轻量稳定（不触 depsgraph、不随网格规模放大、
    同一输入不反复触发重建）、armature 姿态摘要每代共享一次。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()

    def setUp(self):
        self.mod._PREVIEW_DIGEST_CACHE.clear()
        self.mod._PREVIEW_DIGEST_GENERATION = 0
        self.mod._PREVIEW_SIG_ORIG = None
        self._orig_context = getattr(self.mod.bpy, "context", None)
        self.mod.bpy.context = types.SimpleNamespace(
            active_object=None, selected_objects=())

    def tearDown(self):
        self.mod.bpy.context = self._orig_context
        self.mod._PREVIEW_DIGEST_CACHE.clear()
        self.mod._PREVIEW_DIGEST_GENERATION = 0

    def _make_rig(self, n_bones=48, n_ik=16):
        """48 骨 armature + 16 个 IK 约束目标，矩阵多样。"""
        rng = np.random.default_rng(5)
        bones = []
        for i in range(n_bones):
            m = np.eye(4)
            m[:3, :3] = rng.normal(size=(3, 3)) * 0.3
            m[:3, 3] = rng.normal(size=3)
            b = types.SimpleNamespace(name=f"B{i}", matrix=m, constraints=[])
            if i < n_ik:
                tgt = types.SimpleNamespace(name=f"T{i}",
                                            matrix_world=np.eye(4) + i * 1e-3)
                b.constraints = [types.SimpleNamespace(target=tgt)]
            bones.append(b)
        pose = types.SimpleNamespace(bones=bones)
        arm = types.SimpleNamespace(name="Arm01",
                                    data=types.SimpleNamespace(pose_position="POSE"),
                                    pose=pose)
        return arm, bones

    def _make_target(self, name, armature=None, n_keys=3):
        mesh = types.SimpleNamespace(vertices=range(40))
        sk = types.SimpleNamespace(key_blocks=[
            types.SimpleNamespace(name=f"Key{i}", value=0.0, mute=False)
            for i in range(n_keys)
        ])
        mesh.shape_keys = sk
        obj = types.SimpleNamespace(name=name, name_full=name, type="MESH",
                                    matrix_world=np.eye(4), data=mesh)
        if armature is not None:
            obj.modifiers = [types.SimpleNamespace(type="ARMATURE", object=armature)]
        return obj

    def _make_node(self, targets, tree="Tree", name="Drag"):
        node = _make_node(self.mod)
        object.__setattr__(node, "preview_weights", True)
        if len(targets) == 1:
            object.__setattr__(node, "preview_target", targets[0])
            object.__setattr__(node, "preview_collection", None)
        else:
            object.__setattr__(node, "preview_target", None)
            object.__setattr__(node, "preview_collection",
                               types.SimpleNamespace(name="Col",
                                                     all_objects=list(targets)))
        object.__setattr__(node, "id_data", types.SimpleNamespace(name=tree))
        object.__setattr__(node, "name", name)
        empty = types.SimpleNamespace(
            name="SSMT_DragZone_0",
            matrix_world=np.eye(4),
            ssmt_drag_zone=types.SimpleNamespace(
                enabled=True, brush_strength=1.0, brush_falloff_k=4.6,
                propagate=True, include_objects=[]))
        object.__setattr__(node, "zone_objects",
                           [types.SimpleNamespace(zone_object=empty)])
        # t12：区域空物体须被选中才进签名/重建路径（否则签名最小化，
        # 性能测试测的正是选中后的完整路径成本）
        self.mod.bpy.context.selected_objects = [empty]
        return node

    def test_signature_does_not_touch_depsgraph(self):
        """tick 签名路径零 depsgraph 求值：签名/重复签名不调用
        evaluated_depsgraph_get 与 target.evaluated_get。"""
        mod = self.mod
        arm, _bones = self._make_rig()
        target = self._make_target("MeshRig", armature=arm)
        node = self._make_node([target])
        ctx = mod.bpy.context
        calls = []
        orig_get = getattr(ctx, "evaluated_depsgraph_get", None)
        ctx.evaluated_depsgraph_get = lambda: calls.append("deps") or object()
        target.evaluated_get = lambda deps: calls.append("evaluated") or None
        try:
            mod._preview_signature(node)
            mod._preview_signature(node)
        finally:
            if orig_get is not None:
                ctx.evaluated_depsgraph_get = orig_get
            else:
                try:
                    del ctx.evaluated_depsgraph_get
                except AttributeError:
                    pass
        self.assertEqual(calls, [])

    def test_repeated_idle_ticks_do_not_rebuild(self):
        """同一稳定输入连续 tick：仅首 tick 建立预览（1 次重建），
        签名稳定 → 后续 tick 不触发额外重建（重建计数有上界）。"""
        mod = self.mod
        arm, _bones = self._make_rig()
        target = self._make_target("MeshRig", armature=arm)
        node = self._make_node([target])
        mod._preview_sig_cache = None
        mod._preview_pending_signature = None
        mod._preview_pending_since = None
        rebuilds = []
        clock = iter([0.0, 0.2, 0.4, 0.6, 0.8])
        original = (
            mod._collect_preview_nodes,
            mod._ensure_preview_handler,
            mod._rebuild_preview_batches,
            mod._preview_now,
        )
        mod._collect_preview_nodes = lambda: [node]
        mod._ensure_preview_handler = lambda: None
        mod._rebuild_preview_batches = lambda nodes: rebuilds.append(list(nodes))
        mod._preview_now = lambda: next(clock)
        try:
            for _ in range(5):
                mod._preview_tick()
        finally:
            (mod._collect_preview_nodes,
             mod._ensure_preview_handler,
             mod._rebuild_preview_batches,
             mod._preview_now) = original
            mod._preview_sig_cache = None
            mod._preview_pending_signature = None
            mod._preview_pending_since = None
        self.assertEqual(len(rebuilds), 1)

    def test_signature_stable_across_generations_unless_pose_changes(self):
        """重复取签名相等（同代与跨代均稳定）；仅真实姿态变化后（下一 tick）
        签名变化。"""
        mod = self.mod
        arm, bones = self._make_rig()
        target = self._make_target("MeshRig", armature=arm)
        node = self._make_node([target])
        sig1 = mod._preview_signature(node)
        self.assertEqual(sig1, mod._preview_signature(node))
        mod._PREVIEW_DIGEST_GENERATION += 1  # 下一 tick，静态姿态
        sig2 = mod._preview_signature(node)
        self.assertEqual(sig1, sig2)
        bones[3].matrix = np.eye(4) + 0.01  # 真实姿态变化
        mod._PREVIEW_DIGEST_GENERATION += 1
        self.assertNotEqual(sig1, mod._preview_signature(node))

    def test_armature_digest_cache_bounded_per_generation(self):
        """多 target 共享同一 armature：同代内骨架摘要只缓存一项（O(T×B)→O(B)），
        缓存项数有上界；代推进后重新计算。"""
        mod = self.mod
        arm, _bones = self._make_rig()
        targets = [self._make_target(f"M{i}", armature=arm) for i in range(6)]
        node = self._make_node(targets)
        mod._preview_signature(node)
        self.assertEqual(len(mod._PREVIEW_DIGEST_CACHE), 1)
        mod._PREVIEW_DIGEST_GENERATION += 1
        mod._preview_signature(node)
        # 同代内仍只有该 armature 一项（无泄漏、无逐 target 增长）
        self.assertEqual(len(mod._PREVIEW_DIGEST_CACHE), 1)


class DragNodePreviewEventDrivenTests(unittest.TestCase):
    """事件驱动失效（t12）：depsgraph_update_post 置 dirty + 空闲 tick O(1)
    （零签名/零骨骼遍历/零重建），选中变化轮询兜底，安全阀周期性全量签名。
    桩环境用假 bpy.app.handlers.depsgraph_update_post 列表模拟。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()

    def setUp(self):
        mod = self.mod
        self._orig_context = getattr(mod.bpy, "context", None)
        mod.bpy.context = types.SimpleNamespace(active_object=None, selected_objects=())
        self._orig_app = getattr(mod.bpy, "app", None)
        self.handler_list = []
        mod.bpy.app = types.SimpleNamespace(
            handlers=types.SimpleNamespace(depsgraph_update_post=self.handler_list))
        self._saved = (
            mod._preview_deps_registered, mod._preview_dirty,
            mod._preview_last_selection, mod._preview_safety_count,
            mod._PREVIEW_RELEVANT_IDS,
            mod._preview_sig_cache, mod._preview_pending_signature,
            mod._preview_pending_since,
        )
        mod._preview_deps_registered = False
        mod._preview_dirty = True
        mod._preview_last_selection = None
        mod._preview_safety_count = 0
        mod._PREVIEW_RELEVANT_IDS = None
        mod._preview_sig_cache = None
        mod._preview_pending_signature = None
        mod._preview_pending_since = None
        mod._PREVIEW_DIGEST_CACHE.clear()
        mod._PREVIEW_DIGEST_GENERATION = 0

    def tearDown(self):
        mod = self.mod
        (mod._preview_deps_registered, mod._preview_dirty,
         mod._preview_last_selection, mod._preview_safety_count,
         mod._PREVIEW_RELEVANT_IDS,
         mod._preview_sig_cache, mod._preview_pending_signature,
         mod._preview_pending_since) = self._saved
        mod.bpy.context = self._orig_context
        mod.bpy.app = self._orig_app
        mod._PREVIEW_DIGEST_CACHE.clear()
        mod._PREVIEW_DIGEST_GENERATION = 0

    def _zone_empty(self, name="SSMT_DragZone_0"):
        return types.SimpleNamespace(
            name=name, matrix_world=np.eye(4),
            ssmt_drag_zone=types.SimpleNamespace(
                enabled=True, brush_strength=1.0, brush_falloff_k=4.6,
                propagate=True, include_objects=[]))

    def _make_node(self):
        node = _make_node(self.mod)
        object.__setattr__(node, "preview_weights", True)
        object.__setattr__(node, "preview_target", types.SimpleNamespace(
            name="Mesh", matrix_world=np.eye(4),
            data=types.SimpleNamespace(vertices=range(3))))
        object.__setattr__(node, "preview_collection", None)
        object.__setattr__(node, "id_data", types.SimpleNamespace(name="Tree"))
        object.__setattr__(node, "name", "Drag")
        empty = self._zone_empty()
        object.__setattr__(node, "zone_objects",
                           [types.SimpleNamespace(zone_object=empty)])
        self.mod.bpy.context.selected_objects = [empty]
        return node, empty

    def _tick(self, node, rebuilds, clock):
        """受控桩下跑一拍 _preview_tick（收集/绘制/重建/时钟全部替换）。"""
        mod = self.mod
        original = (mod._collect_preview_nodes, mod._ensure_preview_handler,
                    mod._rebuild_preview_batches, mod._preview_now)
        mod._collect_preview_nodes = lambda: [node]
        mod._ensure_preview_handler = lambda: None
        mod._rebuild_preview_batches = lambda nodes: rebuilds.append(list(nodes))
        mod._preview_now = lambda: next(clock)
        try:
            mod._preview_tick()
        finally:
            (mod._collect_preview_nodes, mod._ensure_preview_handler,
             mod._rebuild_preview_batches, mod._preview_now) = original

    def _sig_spy(self):
        """包装真实 _preview_signature 记录调用次数；返回 (calls, restore)。"""
        mod = self.mod
        orig = mod._preview_signature
        calls = []

        def spy(n):
            calls.append(1)
            return orig(n)

        mod._preview_signature = spy
        return calls, lambda: setattr(mod, "_preview_signature", orig)

    def test_handler_register_unregister_idempotent(self):
        """注册/注销幂等；_preview_cleanup 一并注销 deps handler（无泄漏）。"""
        mod = self.mod
        mod._ensure_preview_deps_handler()
        self.assertTrue(mod._preview_deps_registered)
        self.assertEqual(self.handler_list.count(mod._preview_depsgraph_update), 1)
        mod._ensure_preview_deps_handler()  # 重复注册防护
        self.assertEqual(self.handler_list.count(mod._preview_depsgraph_update), 1)
        mod._remove_preview_deps_handler()
        self.assertFalse(mod._preview_deps_registered)
        self.assertNotIn(mod._preview_depsgraph_update, self.handler_list)
        self.assertTrue(mod._preview_dirty)  # 注销后保守置脏
        # cleanup 路径同样注销
        mod._ensure_preview_deps_handler()
        mod._preview_cleanup()
        self.assertNotIn(mod._preview_depsgraph_update, self.handler_list)
        self.assertFalse(mod._preview_deps_registered)

    def test_idle_ticks_event_driven_skip_signature_and_rebuild(self):
        """事件驱动空闲 tick = O(1)：首拍建立后，连续干净 tick 不计算签名、
        不重建（零骨骼/矩阵/depsgraph 访问）。"""
        mod = self.mod
        node, _empty = self._make_node()
        mod._ensure_preview_deps_handler()
        rebuilds = []
        calls, restore = self._sig_spy()
        clock = iter(range(100))
        try:
            self._tick(node, rebuilds, clock)   # 首拍：dirty → 全量签名 + 建立预览
            self.assertFalse(mod._preview_dirty)
            for _ in range(3):                  # 干净 tick：O(1) 早退
                self._tick(node, rebuilds, clock)
        finally:
            restore()
        self.assertEqual(len(rebuilds), 1)
        self.assertEqual(len(calls), 1)         # 签名只在首拍计算一次

    def test_relevant_update_marks_dirty_and_debounced_rebuild(self):
        """相关对象（区域空物体）的 depsgraph 更新 → 置 dirty → 下一拍重算签名；
        签名实变（移动空物体）→ 防抖后重建。"""
        mod = self.mod
        node, empty = self._make_node()
        mod._ensure_preview_deps_handler()
        rebuilds = []
        calls, restore = self._sig_spy()
        clock = iter(range(100))
        try:
            self._tick(node, rebuilds, clock)
            self.assertEqual(len(calls), 1)
            # 无关对象更新 → 不置脏
            mod._preview_depsgraph_update(types.SimpleNamespace(
                updates=[types.SimpleNamespace(id=types.SimpleNamespace(name="Other"))]))
            self.assertFalse(mod._preview_dirty)
            self._tick(node, rebuilds, clock)
            self.assertEqual(len(calls), 1)     # 仍空闲
            # 相关对象（区域空物体）更新 → 置脏 → 下一拍重算签名（内容未变则不重建）
            mod._preview_depsgraph_update(types.SimpleNamespace(
                updates=[types.SimpleNamespace(id=empty)]))
            self.assertTrue(mod._preview_dirty)
            self._tick(node, rebuilds, clock)
            self.assertEqual(len(calls), 2)
            self.assertEqual(len(rebuilds), 1)
            # 真实移动空物体 + 更新事件 → 签名变化 → 防抖窗口后重建一次
            m = np.eye(4)
            m[0, 3] = 5.0
            empty.matrix_world = m
            mod._preview_depsgraph_update(types.SimpleNamespace(
                updates=[types.SimpleNamespace(id=empty)]))
            self._tick(node, rebuilds, clock)   # 记录待处理签名（clock 步进 1）
            self._tick(node, rebuilds, clock)   # 距首变 >0.6s → 重建
        finally:
            restore()
        self.assertEqual(len(rebuilds), 2)

    def test_safety_valve_forces_signature_after_n_clean_ticks(self):
        """安全阀：每 _PREVIEW_SAFETY_TICKS 个干净 tick 强制一次全量签名
        （兜 driver 漏报），但无实际变化时不重建。"""
        mod = self.mod
        node, _empty = self._make_node()
        mod._ensure_preview_deps_handler()
        rebuilds = []
        calls, restore = self._sig_spy()
        clock = iter(range(1000))
        try:
            self._tick(node, rebuilds, clock)          # 首拍建立
            for _ in range(mod._PREVIEW_SAFETY_TICKS): # 干净 tick × N
                self._tick(node, rebuilds, clock)
        finally:
            restore()
        self.assertEqual(len(calls), 2)   # 首拍 1 次 + 安全阀 1 次
        self.assertEqual(len(rebuilds), 1)

    def test_stub_without_handlers_falls_back_to_polling(self):
        """无 app.handlers（stub/旧版 Blender）→ 静默降级回 t8 轮询语义：
        每拍都算全量签名，防抖语义不变。"""
        mod = self.mod
        mod.bpy.app = types.SimpleNamespace()  # 无 handlers
        node, _empty = self._make_node()
        mod._ensure_preview_deps_handler()
        self.assertFalse(mod._preview_deps_registered)  # 降级：未注册
        rebuilds = []
        calls, restore = self._sig_spy()
        clock = iter(range(100))
        try:
            for _ in range(3):
                self._tick(node, rebuilds, clock)
        finally:
            restore()
        self.assertEqual(len(calls), 3)   # 轮询：每拍都付签名
        self.assertEqual(len(rebuilds), 1)

    def test_refresh_relevant_ids_covers_preview_datablocks(self):
        """相关键集覆盖：目标物体/网格/形态键/骨架+骨架数据/区域空物体/集合；
        无关对象不在集内。"""
        mod = self.mod
        node, empty = self._make_node()
        target = node.preview_target
        target.type = "MESH"
        target.name_full = target.name
        mesh = target.data
        mesh.shape_keys = types.SimpleNamespace(key_blocks=[])
        arm = types.SimpleNamespace(name="Arm",
                                    data=types.SimpleNamespace(pose_position="POSE"))
        target.modifiers = [types.SimpleNamespace(type="ARMATURE", object=arm)]
        collection = types.SimpleNamespace(name="Col", all_objects=[target])
        object.__setattr__(node, "preview_collection", collection)
        mod._preview_refresh_relevant_ids([node])
        relevant = mod._PREVIEW_RELEVANT_IDS
        for datablock in (target, mesh, mesh.shape_keys, arm, arm.data,
                          empty, collection):
            self.assertIn(mod._preview_rel_key(datablock), relevant)
        self.assertNotIn(mod._preview_rel_key(
            types.SimpleNamespace(name="Other")), relevant)


class DragNodeZoneFilterTests(unittest.TestCase):
    """球级沿表面扩散开关 + 包含物体列表过滤（烘焙/预览侧）。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()

    def _settings(self, propagate=None, include_names=()):
        settings = types.SimpleNamespace(
            enabled=True,
            brush_strength=1.0,
            brush_falloff_k=4.6,
            radius=0.0,
            strength=0.0,
            max_offset=0.0,
            falloff=0.0,
            damping=0.0,
            grabbable=True,
        )
        if propagate is not None:
            settings.propagate = propagate
        if include_names:
            settings.include_objects = [
                types.SimpleNamespace(object=types.SimpleNamespace(name=name))
                for name in include_names
            ]
        return settings

    def test_zone_propagate_defaults_on(self):
        """节点级总开关已移除：旧数据无 propagate 属性时默认开启沿表面扩散；
        球级 propagate 显式关闭时覆盖默认。"""
        mod = self.mod
        node = _make_node(self.mod)
        settings = self._settings()  # 旧数据：无 propagate 属性
        self.assertTrue(mod._zone_propagate(settings, node))
        # 球级开关显式控制
        self.assertTrue(mod._zone_propagate(self._settings(propagate=True), node))
        self.assertFalse(mod._zone_propagate(self._settings(propagate=False), node))

    def test_zone_allowed_by_target(self):
        mod = self.mod
        self.assertTrue(mod._zone_allowed_by_target(self._settings(), "A"))  # 空列表 = 全部允许
        settings = self._settings(include_names=["A"])
        self.assertTrue(mod._zone_allowed_by_target(settings, "A"))
        self.assertFalse(mod._zone_allowed_by_target(settings, "B"))
        # 导出追加后缀：包含 Body_copy 也能命中 Body（运行时后缀剥离）
        runtime = self._settings(include_names=["Body_copy"])
        self.assertTrue(mod._zone_allowed_by_target(runtime, "Body"))
        # Blender 去重后缀 .001 是正常物体名的一部分，不折叠：
        # 包含 Body.001 命中 Body.001，但不再连带命中 Body（独立网格互不串）
        suffix = self._settings(include_names=["Body.001"])
        self.assertFalse(mod._zone_allowed_by_target(suffix, "Body"))
        self.assertTrue(mod._zone_allowed_by_target(suffix, "Body.001"))

    def test_zone_allowed_vertex_mask_treats_dot001_as_distinct_mesh(self):
        """ZZMI 合并网格回归：导出 ini 里 .001 后缀是真实独立 draw（独立 IB 区间），
        包含 .001 只放行该段顶点，不得折叠到基础名把整组件全放行。"""
        mod = self.mod
        triangles = np.array([[0, 1, 2], [3, 4, 5]], dtype=np.int64)
        tri_part_names = np.array([
            "LOD0.b30db54e-7383-0_copy",
            "LOD0.b30db54e-7383-0.001_copy",
        ], dtype=object)
        # 只包含 .001 小物体 → 只放行第二段
        settings_small = self._settings(include_names=["LOD0.b30db54e-7383-0.001"])
        mask_small = mod._zone_allowed_vertex_mask(settings_small, 6, triangles, tri_part_names)
        np.testing.assert_array_equal(
            mask_small, np.array([False, False, False, True, True, True])
        )
        # 只包含主物体 → 只放行第一段
        settings_main = self._settings(include_names=["LOD0.b30db54e-7383-0"])
        mask_main = mod._zone_allowed_vertex_mask(settings_main, 6, triangles, tri_part_names)
        np.testing.assert_array_equal(
            mask_main, np.array([True, True, True, False, False, False])
        )

    def test_collider_vertex_mask_treats_dot001_as_distinct_mesh(self):
        """碰撞体同规约：.001 后缀按真实独立网格精确匹配，不折叠到基础名。"""
        mod = self.mod
        triangles = np.array([[0, 1, 2], [3, 4, 5]], dtype=np.int64)
        tri_part_names = np.array([
            "LOD0.b30db54e-7383-0_copy",
            "LOD0.b30db54e-7383-0.001_copy",
        ], dtype=object)
        colliders = [
            types.SimpleNamespace(object=types.SimpleNamespace(
                name="LOD0.b30db54e-7383-0.001", name_full=None))
        ]
        settings = types.SimpleNamespace(
            enabled=True, brush_strength=1.0, brush_falloff_k=4.6,
            radius=0.0, strength=0.0, max_offset=0.0, falloff=0.0, damping=0.0,
            grabbable=True, include_objects=[], collider_objects=colliders,
            collision_override=0,
        )
        mask = mod._collider_vertex_mask(settings, 6, triangles, tri_part_names)
        np.testing.assert_array_equal(mask, np.array([False, False, False, True, True, True]))

    def test_include_list_with_invalid_entries_defaults_to_all(self):
        """包含列表项存在但物体指针已失效（如物体被删除）→ 视为无有效过滤，
        默认作用于全部物体，预览不会被误过滤成空白。"""
        mod = self.mod
        settings = self._settings(include_names=["A"])
        settings.include_objects[0].object = None  # 模拟物体被删除后的残留项
        self.assertTrue(mod._zone_allowed_by_target(settings, "A"))
        self.assertTrue(mod._zone_allowed_by_target(settings, "B"))
        self.assertIsNone(
            mod._zone_allowed_vertex_mask(settings, 6, np.array([[0, 1, 2]]), np.array(["A"], dtype=object))
        )

    def test_empty_collection_property_defaults_to_all(self):
        """模拟 bpy CollectionProperty 边界行为：空列表即使布尔求值为真，
        也必须按“未添加包含物体 = 全部物体”处理，不能过滤掉任何目标。"""
        mod = self.mod

        class FakeCollection:
            """len=0 但 bool 为 True（模拟对 bpy 集合 bool 语义不确定的极端情况）。"""
            def __len__(self):
                return 0

            def __bool__(self):
                return True

            def __iter__(self):
                return iter(())

        settings = self._settings()
        settings.include_objects = FakeCollection()
        self.assertTrue(mod._zone_allowed_by_target(settings, "A"))
        self.assertTrue(mod._zone_allowed_by_target(settings, "B"))
        # 预览链路：空列表 → 全部目标都保留，不被过滤成空白
        node = _make_node(self.mod)
        object.__setattr__(node, "mask_plateau", 0.0)
        ball_matrix = np.eye(4)
        ball_matrix[:3, 3] = (1.0, 0.0, 0.0)
        empty = types.SimpleNamespace(
            name="SSMT_DragZone_0", matrix_world=ball_matrix,
            ssmt_drag_zone=settings,
        )
        zones = [(empty, settings)]
        verts = np.array([[0.9, 0.0, 0.0], [1.0, 0.0, 0.0], [1.1, 0.0, 0.0]], dtype=np.float64)
        tris = np.array([[0, 1, 2]], dtype=np.int64)
        field = mod._preview_target_field(node, verts, tris, zones, 0.0, target_name="A")
        self.assertGreater(float(field.max()), 0.3)

    def test_zone_allowed_vertex_mask_filters_by_part_names(self):
        mod = self.mod
        triangles = np.array([[0, 1, 2], [3, 4, 5]], dtype=np.int64)
        tri_part_names = np.array(["Body", "Head"], dtype=object)
        settings = self._settings(include_names=["Body"])
        mask = mod._zone_allowed_vertex_mask(settings, 6, triangles, tri_part_names)
        np.testing.assert_array_equal(mask, np.array([True, True, True, False, False, False]))
        # 无注释部件（None）无法判定归属 → 按允许处理，不被列表清零
        tri_part_names_none = np.array(["Body", None], dtype=object)
        mask_none = mod._zone_allowed_vertex_mask(settings, 6, triangles, tri_part_names_none)
        np.testing.assert_array_equal(mask_none, np.ones(6, dtype=bool))
        # 空列表 / 无拓扑 → None（全允许）
        self.assertIsNone(mod._zone_allowed_vertex_mask(self._settings(), 6, triangles, tri_part_names))
        self.assertIsNone(mod._zone_allowed_vertex_mask(settings, 6, None, None))

    def test_zone_allowed_vertex_mask_matches_export_mesh_comment_names(self):
        # 导出 IB 的 mesh 注释名只是场景物体名末尾追加 _copy 后缀；
        # 完整 LOD/IB 前缀必须保留，仅剥掉导出追加的后缀后比较。
        mod = self.mod
        triangles = np.array([[0, 1, 2], [3, 4, 5]], dtype=np.int64)
        tri_part_names = np.array([
            "LOD0.b1870eee-42927-0.服装03丝袜.004_透明0.75_copy",
            "LOD0.b1870eee-42927-0.头部.002_copy",
        ], dtype=object)
        settings = self._settings(include_names=["LOD0.b1870eee-42927-0.服装03丝袜.004_透明0.75"])
        mask = mod._zone_allowed_vertex_mask(settings, 6, triangles, tri_part_names)
        np.testing.assert_array_equal(mask, np.array([True, True, True, False, False, False]))

    def test_zone_allowed_vertex_mask_keeps_ib_prefix_distinct(self):
        # 同名物体但 IB 前缀不同时不得互相命中，前缀用于区分 IB。
        mod = self.mod
        triangles = np.array([[0, 1, 2]], dtype=np.int64)
        tri_part_names = np.array([
            "LOD0.b1870eee-42927-0.服装03丝袜.004_透明0.75_copy",
        ], dtype=object)
        settings = self._settings(include_names=["LOD0.aaaaaaa-42927-0.服装03丝袜.004_透明0.75"])
        mask = mod._zone_allowed_vertex_mask(settings, 3, triangles, tri_part_names)
        np.testing.assert_array_equal(mask, np.zeros(3, dtype=bool))

    def test_preview_target_field_include_list_filters_targets(self):
        """预览单物体：未包含在列表里的目标网格即使被球命中也不加权。"""
        mod = self.mod
        node = _make_node(self.mod)
        object.__setattr__(node, "mask_plateau", 0.0)
        ball_matrix = np.eye(4)
        ball_matrix[:3, 3] = (1.0, 0.0, 0.0)
        empty = types.SimpleNamespace(
            name="SSMT_DragZone_0",
            matrix_world=ball_matrix,
            ssmt_drag_zone=self._settings(include_names=["A"]),
        )
        zones = [(empty, empty.ssmt_drag_zone)]
        verts = np.array([[0.9, 0.0, 0.0], [1.0, 0.0, 0.0], [1.1, 0.0, 0.0]], dtype=np.float64)
        tris = np.array([[0, 1, 2]], dtype=np.int64)
        field_a = mod._preview_target_field(node, verts, tris, zones, 0.0, target_name="A")
        field_b = mod._preview_target_field(node, verts, tris, zones, 0.0, target_name="B")
        self.assertGreater(float(field_a.max()), 0.3)
        np.testing.assert_array_equal(field_b, np.zeros(3))

    def test_preview_merged_mesh_include_list_blocks_non_included_mesh(self):
        """集合预览：A、B 焊接共享接缝，列表只含 A → 权重不传播进 B。"""
        mod = self.mod
        node = _make_node(self.mod)
        object.__setattr__(node, "mask_plateau", 0.0)
        empty = types.SimpleNamespace(
            name="SSMT_DragZone_0",
            matrix_world=np.diag([0.25, 0.25, 0.25, 1.0]),
            ssmt_drag_zone=self._settings(include_names=["A"]),
        )
        zones = [(empty, empty.ssmt_drag_zone)]
        verts_a = np.array([[0.1, 0.0, 0.0], [0.15, 0.0, 0.0], [0.2, 0.0, 0.0]], dtype=np.float64)
        tris_a = np.array([[0, 1, 2]], dtype=np.int64)
        verts_b = np.array([[0.2, 0.0, 0.0], [0.2, 0.1, 0.0], [0.2, 0.2, 0.0]], dtype=np.float64)
        tris_b = np.array([[0, 1, 2]], dtype=np.int64)
        fields = mod._preview_merged_mesh(
            node,
            [(verts_a, tris_a), (verts_b, tris_b)],
            zones,
            0.0,
            mesh_names=["A", "B"],
        )
        self.assertGreater(float(fields[0].max()), 0.0)   # A 命中
        np.testing.assert_array_equal(fields[1], np.zeros(3))  # B 不被传播


class DragNodeMirrorTests(unittest.TestCase):
    """非镜像工作流：区域空物体矩阵 X 镜像补偿（掩码左右颠倒回归）。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()

    def _zone_empty(self, loc=(1.0, 0.0, 0.0)):
        m = np.eye(4)
        m[0, 3], m[1, 3], m[2, 3] = loc
        settings = types.SimpleNamespace(brush_strength=1.0, brush_falloff_k=4.6)
        return types.SimpleNamespace(name="SSMT_DragZone_0", matrix_world=m,
                                     ssmt_drag_zone=settings)

    def test_mirror_flips_ball_side(self):
        mod = self.mod
        node = _make_node(mod)
        settings = self._zone_empty().ssmt_drag_zone
        empty = self._zone_empty()
        # 顶点在 X±1.5，球在 X+1（半径 1，d<1 有效）→ 只有 X+1.5 命中
        positions = np.array([[1.5, 0.0, 0.0], [-1.5, 0.0, 0.0]], dtype=np.float32)
        field = node._evaluate_zone_field(positions, empty, settings, None, None)
        self.assertGreater(float(field[0]), 0.0)
        self.assertEqual(float(field[1]), 0.0)
        # 施加 X 镜像 → 球等效到 X-1 → 只有 X-1.5 命中
        mirror = np.diag([-1.0, 1.0, 1.0, 1.0])
        field_m = node._evaluate_zone_field(positions, empty, settings, None, mirror)
        self.assertEqual(float(field_m[0]), 0.0)
        self.assertGreater(float(field_m[1]), 0.0)

    def test_mirror_composes_with_ref_matrix_inv(self):
        mod = self.mod
        node = _make_node(mod)
        settings = self._zone_empty().ssmt_drag_zone
        empty = self._zone_empty()
        # 参考矩阵：X 平移 -0.5 → 球（镜像后 X-1）落到 X-1.5
        ref_inv = np.eye(4)
        ref_inv[0, 3] = -0.5
        mirror = np.diag([-1.0, 1.0, 1.0, 1.0])
        positions = np.array([[1.75, 0.0, 0.0], [-1.75, 0.0, 0.0]], dtype=np.float32)
        field = node._evaluate_zone_field(positions, empty, settings, ref_inv, mirror)
        self.assertEqual(float(field[0]), 0.0)
        self.assertGreater(float(field[1]), 0.0)

    def test_compensation_detection_by_scene_and_ref(self):
        mod = self.mod
        bpy = mod.bpy
        marker = "_ssmt_non_mirror_workflow_processed"
        node = _make_node(mod)
        # 场景网格带标记 → 补偿
        bpy.data.objects = [
            types.SimpleNamespace(type="MESH", get=lambda k, d=False, m=marker: k == m),
            types.SimpleNamespace(type="EMPTY", get=lambda k, d=False: False),
        ]
        mir = node._get_non_mirror_mirror()
        self.assertIsNotNone(mir)
        np.testing.assert_allclose(mir, np.diag([-1.0, 1.0, 1.0, 1.0]))
        # 无标记 → None
        bpy.data.objects = [types.SimpleNamespace(type="MESH", get=lambda k, d=False: False)]
        self.assertIsNone(node._get_non_mirror_mirror())
        # 参考物体（网格）带标记 → 优先命中
        ref = types.SimpleNamespace(type="MESH", get=lambda k, d=False, m=marker: k == m)
        object.__setattr__(node, "bake_reference_object", ref)
        self.assertIsNotNone(node._get_non_mirror_mirror())


class DragNodePlateauTests(unittest.TestCase):
    """权重平台化：高斯尖峰 → 平台形 mask（拖拽以鼠标命中点为锚，不被权重峰吸走）。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()

    def _node(self):
        return _make_node(self.mod)

    def _unit_ball(self):
        m = np.eye(4)  # 单位球（缩放 1 = 半径 1），球心在原点
        return m

    def test_plateau_zero_degenerates_to_gaussian(self):
        node = self._node()
        # 沿 X 轴采点（d = |x|）
        pts = np.array([[0.0, 0.0, 0.0], [0.3, 0.0, 0.0], [0.6, 0.0, 0.0], [0.9, 0.0, 0.0]], dtype=np.float64)
        g = node._plateau_field(pts, self._unit_ball(), 1.0, 4.6, 0.0)
        ref = self.mod.gb_core.gaussian_field(pts, self._unit_ball(), 1.0, 4.6)
        np.testing.assert_allclose(g, ref, atol=1e-12)

    def test_plateau_flattens_center_and_smooth_edge(self):
        node = self._node()
        pts = np.array([[0.0, 0.0, 0.0], [0.5, 0.0, 0.0], [0.7, 0.0, 0.0],
                        [0.85, 0.0, 0.0], [1.0, 0.0, 0.0], [1.2, 0.0, 0.0]], dtype=np.float64)
        f = node._plateau_field(pts, self._unit_ball(), 1.0, 4.6, 0.7)
        # d ≤ 0.7：满强度平台（对比高斯在 0.5 处已衰减到 ~0.35）
        self.assertAlmostEqual(float(f[0]), 1.0, places=6)
        self.assertAlmostEqual(float(f[1]), 1.0, places=6)
        self.assertAlmostEqual(float(f[2]), 1.0, places=6)
        # 过渡带 0.7~1.0：单调降到 0
        self.assertGreater(float(f[3]), 0.0)
        self.assertLess(float(f[3]), 1.0)
        self.assertEqual(float(f[4]), 0.0)  # d=1 硬截止
        self.assertEqual(float(f[5]), 0.0)  # 球外
        # 高斯对照：0.5 处远小于平台值（证明平台化确实展平了尖峰）
        g = self.mod.gb_core.gaussian_field(pts, self._unit_ball(), 1.0, 4.6)
        self.assertLess(float(g[1]), 0.5)

    def test_plateau_used_by_bake_and_preview_path(self):
        node = self._node()
        object.__setattr__(node, "mask_plateau", 0.7)  # 默认已回退 0（纯高斯），此处显式开平台化
        settings = types.SimpleNamespace(brush_strength=1.0, brush_falloff_k=4.6)
        empty = types.SimpleNamespace(name="z", matrix_world=self._unit_ball(),
                                      ssmt_drag_zone=settings)
        pts = np.array([[0.5, 0.0, 0.0], [2.0, 0.0, 0.0]], dtype=np.float32)
        # _evaluate_zone_field 走平台化（node.mask_plateau=0.7）
        f = node._evaluate_zone_field(pts, empty, settings, None, None)
        self.assertAlmostEqual(float(f[0]), 1.0, places=5)
        self.assertEqual(float(f[1]), 0.0)
        # plateau=0 时退化为高斯：0.5 处 < 1
        object.__setattr__(node, "mask_plateau", 0.0)
        f0 = node._evaluate_zone_field(pts, empty, settings, None, None)
        self.assertLess(float(f0[0]), 0.5)

    def test_radius_scale_mismatch_warns(self):
        node = self._node()
        # 球 scale=0.1
        m = np.eye(4) * 0.1
        m[3, 3] = 1.0
        # radius=0.5 → 5× > 2.5× → 警告
        big = types.SimpleNamespace(
            name="z_big", matrix_world=m,
            ssmt_drag_zone=types.SimpleNamespace(radius=0.5))
        self.assertEqual(node._check_zone_radius_scale([big]), 1)
        # radius=0.15 → 1.5× → 不警告
        ok = types.SimpleNamespace(
            name="z_ok", matrix_world=m,
            ssmt_drag_zone=types.SimpleNamespace(radius=0.15))
        self.assertEqual(node._check_zone_radius_scale([ok]), 0)
        # radius=0 → 继承回退 0.25 → 恰为 2.5× 边界（严格大于才警告）→ 不警告
        inherit = types.SimpleNamespace(
            name="z_inherit", matrix_world=m,
            ssmt_drag_zone=types.SimpleNamespace(radius=0.0))
        self.assertEqual(node._check_zone_radius_scale([inherit]), 0)


class DragNodeGeodesicTests(unittest.TestCase):
    """沿表面传播：_zone_distances 测地路径 vs 体积球回退。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()

    def _mesh(self):
        """front z=0（顶点0-4）、back z=0.5（顶点5-9），片间不连通。"""
        front = np.array([[x * 0.1, 0.0, 0.0] for x in range(5)])
        back = np.array([[x * 0.1, 0.0, 0.5] for x in range(5)])
        verts = np.vstack([front, back]).astype(np.float32)
        tris = np.array([[0, 1, 2], [0, 2, 3], [0, 3, 4],
                         [5, 6, 7], [5, 7, 8], [5, 8, 9]])
        return verts, tris

    def _ball(self, radius=0.6):
        m = np.eye(4) * radius
        m[3, 3] = 1.0
        settings = types.SimpleNamespace(
            brush_strength=1.0, brush_falloff_k=4.6, propagate=True
        )
        return types.SimpleNamespace(name="z", matrix_world=m, ssmt_drag_zone=settings)

    def test_surface_mode_kills_back_strip(self):
        node = _make_node(self.mod)
        verts, tris = self._mesh()
        edges = self.mod.gb_core.edges_from_triangles(tris)
        empty = self._ball()
        f = node._evaluate_zone_field(verts, empty, empty.ssmt_drag_zone, None, None, edges)
        self.assertIsNotNone(f)
        self.assertGreater(float(f[2]), 0.3)                     # 接触面有权重
        np.testing.assert_array_equal(f[5:], np.zeros(5, dtype=np.float32))  # back 全 0

    def test_volume_mode_paints_back_strip(self):
        node = _make_node(self.mod)
        verts, tris = self._mesh()
        edges = self.mod.gb_core.edges_from_triangles(tris)
        empty = self._ball()
        empty.ssmt_drag_zone.propagate = False  # 球级关闭沿表面扩散
        f = node._evaluate_zone_field(verts, empty, empty.ssmt_drag_zone, None, None, edges)
        self.assertGreater(float(f[7]), 0.0)                     # 体积球穿透到 back

    def test_no_topology_falls_back_to_volume(self):
        node = _make_node(self.mod)
        verts, _ = self._mesh()
        empty = self._ball()
        f = node._evaluate_zone_field(verts, empty, empty.ssmt_drag_zone, None, None, None)
        self.assertGreater(float(f[7]), 0.0)                     # 无拓扑 → 体积球


class DragCollisionTests(unittest.TestCase):
    """碰撞检测（防穿模）烘焙与发射的纯逻辑测试。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_drag_module()
        cls.NodeCls = cls.mod.SSMTNode_PostProcess_DragInteraction

    # ---- 辅助 ----

    def _collider_item(self, zone_id=0, collider_names=None, override=0, enabled=True):
        colliders = []
        for name in (collider_names or []):
            obj = types.SimpleNamespace(name=name, name_full=name, type='MESH')
            colliders.append(types.SimpleNamespace(object=obj))
        settings = types.SimpleNamespace(
            enabled=enabled, brush_strength=1.0, brush_falloff_k=4.6,
            radius=0.0, strength=0.0, max_offset=0.0, falloff=0.0, damping=0.0,
            grabbable=True, include_objects=[], collider_objects=colliders,
            collision_override=override,
        )
        empty = types.SimpleNamespace(name=f"zone_{zone_id}", ssmt_drag_zone=settings, matrix_world=np.eye(4))
        return types.SimpleNamespace(zone_id=zone_id, zone_object=empty)

    def _emit_with_collision_grid(self):
        node = _make_node(self.mod, collision_enabled=True)
        sections = _base_sections()
        comps = node._locate_components(sections, ["abc123"])
        comps[0]["collision_grid"] = {
            "bmin": np.array([0.0, 0.0, 0.0], dtype=np.float32),
            "h": 0.1, "h_c": 0.4,
            "dims": (10, 8, 6), "cdims": (3, 2, 2),
        }
        node._emit_sections(sections, comps, "testns")
        return node, sections, comps

    def _emit(self, **props):
        node = _make_node(self.mod, **props)
        sections = _base_sections()
        comps = node._locate_components(sections, ["abc123"])
        node._emit_sections(sections, comps, "testns")
        return node, sections, comps

    # ---- 纯函数烘焙测试 ----

    def test_collider_vertex_normals_face_outward(self):
        verts = np.array([
            [-1, -1, -1], [1, -1, -1], [1, 1, -1], [-1, 1, -1],
            [-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1],
        ], dtype=np.float32)
        tris = np.array([
            [0, 1, 2], [0, 2, 3],
            [4, 6, 5], [4, 7, 6],
            [0, 4, 5], [0, 5, 1],
            [3, 2, 6], [3, 6, 7],
            [0, 3, 7], [0, 7, 4],
            [1, 5, 6], [1, 6, 2],
        ], dtype=np.int64)
        mask = np.ones(8, dtype=bool)
        n = self.NodeCls._collider_vertex_normals(verts, tris, mask, 8)
        dots = np.einsum("ij,ij->i", n, verts.astype(np.float32))
        self.assertTrue(np.all(dots > 0.0), f"法线应朝外，dot={dots}")

    def test_decimate_points_within_budget(self):
        rng = np.random.RandomState(0)
        pts = rng.rand(3000, 3).astype(np.float32)
        nrm = np.ones((3000, 3), dtype=np.float32)
        dec_p, dec_n = self.NodeCls._decimate_collider_points(pts, nrm, 512)
        self.assertLessEqual(len(dec_p), 512)
        self.assertEqual(len(dec_p), len(dec_n))

    def test_decimate_points_noop_when_under_budget(self):
        pts = np.random.RandomState(1).rand(50, 3).astype(np.float32)
        nrm = np.ones((50, 3), dtype=np.float32)
        dec_p, dec_n = self.NodeCls._decimate_collider_points(pts, nrm, 512)
        self.assertEqual(len(dec_p), 50)

    def test_build_collider_grid_invariants(self):
        rng = np.random.RandomState(2)
        pts = rng.rand(1000, 3).astype(np.float32)
        grid = self.NodeCls._build_collider_grid(pts, 0.0, 0.002)
        # count 总和 = 点数
        self.assertEqual(int(grid["fine_cells"][:, 1].sum()), len(grid["sorted_points"]))
        # 非空 cell 的 offset 单调递增（count-sort 布局）
        nonempty = np.flatnonzero(grid["fine_cells"][:, 1] > 0)
        offsets = grid["fine_cells"][nonempty, 0].astype(np.int64)
        self.assertTrue(np.all(np.diff(offsets) > 0))
        # 粗层每 cell 半径非负且质心在有限范围
        self.assertTrue(np.all(grid["coarse_cells"][:, 3] >= 0.0))

    def test_bake_collider_l0_matches_bruteforce(self):
        rng = np.random.RandomState(3)
        positions = rng.rand(50, 3).astype(np.float32)
        points = rng.rand(20, 3).astype(np.float32)
        l0 = self.NodeCls._bake_collider_l0(positions, points, None)
        for i in range(50):
            d = np.linalg.norm(positions[i] - points, axis=1)
            j = int(np.argmin(d))
            self.assertAlmostEqual(float(l0[2 * i, 3]), float(d[j]), places=5)
            np.testing.assert_allclose(l0[2 * i, 0:3], points[j], atol=1e-5)
            expected_n = (positions[i] - points[j]) / max(d[j], 1e-12)
            np.testing.assert_allclose(l0[2 * i + 1, 0:3], expected_n, atol=1e-4)
            self.assertEqual(float(l0[2 * i + 1, 3]), 1.0)

    def test_bake_collider_l0_signed_threshold_with_normals(self):
        """设计语义：传点云法线时 t0 = 导出时刻到表面的带符号距离（阈值），
        n0 = 表面法线（而非指向顶点的方向）。"""
        positions = np.array([
            [0.0, 0.0, 0.05],   # 表面外 5cm（表面在 z=0，外法线 +z）
            [0.0, 0.0, -0.02],  # 导出时已穿透 2cm
        ], dtype=np.float32)
        points = np.array([[0.0, 0.0, 0.0]], dtype=np.float32)
        normals = np.array([[0.0, 0.0, 1.0]], dtype=np.float32)
        l0 = self.NodeCls._bake_collider_l0(positions, points, None, normals=normals)
        # t0 = 带符号表面距离
        self.assertAlmostEqual(float(l0[0, 3]), 0.05, places=5)
        self.assertAlmostEqual(float(l0[2, 3]), -0.02, places=5)
        # n0 = 表面法线（不是 (v-q0) 方向）
        np.testing.assert_allclose(l0[1, 0:3], normals[0], atol=1e-6)
        np.testing.assert_allclose(l0[3, 0:3], normals[0], atol=1e-6)
        self.assertEqual(float(l0[1, 3]), 1.0)
        # 掩码：只烘焙权重顶点
        masked = np.array([True, False], dtype=bool)
        l0m = self.NodeCls._bake_collider_l0(positions, points, masked, normals=normals)
        self.assertEqual(float(l0m[1, 3]), 1.0)   # 顶点0 valid
        self.assertEqual(float(l0m[3, 3]), 0.0)   # 顶点1 未遮 → valid=0

    def test_collider_vertex_mask_selects_objects(self):
        triangles = np.array([[0, 1, 2], [3, 4, 5]], dtype=np.int64)
        tri_part_names = np.array(["Thigh", "Stocking"], dtype=object)
        obj = types.SimpleNamespace(name="Thigh", name_full="Thigh", type='MESH')
        settings = types.SimpleNamespace(collider_objects=[types.SimpleNamespace(object=obj)])
        mask = self.mod._collider_vertex_mask(settings, 6, triangles, tri_part_names)
        np.testing.assert_array_equal(mask, np.array([True, True, True, False, False, False]))

    def test_collider_vertex_mask_empty_list_returns_none(self):
        settings = types.SimpleNamespace(collider_objects=[])
        self.assertIsNone(self.mod._collider_vertex_mask(settings, 6, None, None))

    def test_collect_collider_objects_respects_override_and_enabled(self):
        node = _make_node(self.mod, collision_enabled=True,
                          zone_objects=[self._collider_item(0, ["Thigh"], override=0)])
        objs = node._collect_collider_objects()
        self.assertEqual(len(objs), 1)
        self.assertEqual(objs[0].name, "Thigh")

        forced_off = _make_node(self.mod, collision_enabled=True,
                                zone_objects=[self._collider_item(0, ["Thigh"], override=2)])
        self.assertEqual(forced_off._collect_collider_objects(), [])

        global_off = _make_node(self.mod, collision_enabled=False,
                                zone_objects=[self._collider_item(0, ["Thigh"], override=0)])
        self.assertEqual(global_off._collect_collider_objects(), [])
        self.assertFalse(global_off._collision_enabled_for_component())

    # ---- ini 发射测试 ----

    def test_jiggle_section_emits_collision_bindings_and_params(self):
        node, sections, comps = self._emit_with_collision_grid()
        cn = comps[0]["comp_name"]
        lines = sections[f"[CustomShaderDragJiggle{cn}_testns]"]
        text = "\n".join(lines)
        self.assertIn(f"cs-t76 = ResourceDragColliderPoints_{cn}_testns", text)
        self.assertIn(f"cs-t77 = ResourceDragColliderCellsFine_{cn}_testns", text)
        self.assertIn(f"cs-t78 = ResourceDragColliderCellsCoarse_{cn}_testns", text)
        self.assertIn(f"cs-t79 = ResourceDragColliderVertexL0_{cn}_testns", text)
        # 碰撞参数必须是**一个分量一行**。
        #
        # 2026-09-12 测试版修复：这里原先是
        #     self.assertIn("x101 = 1 0.002 0 0.9", text)
        # 也就是说**测试把错误写法锁死了**，于是它在生成器里活了很久。
        # 「一行四个值」不是简写而是语法错误：`xNNN` 只取一个 float，右边整串被
        # 当成一个表达式分词，tokenise() 遇到连续两个操作数直接抛
        # `Unexpected identifier`，整行作废、x101~x104 全保持 0、
        # 着色器的 `if (COLLISION_PARAMS.x > 0.5)` 永远为假 ——
        # 碰撞解算一次都不执行，且不报错。证据链见 notes/50。
        for expected in (
            # x101: enabled=1 margin=0.002 mode=0(soft) safety=0.9
            "x101 = 1", "y101 = 0.002", "z101 = 0", "w101 = 0.9",
            # x102: bmin.xyz + 细格边长
            "x102 = 0", "y102 = 0", "z102 = 0", "w102 = 0.1",
            # x103: 细格维度 xyz + 粗格边长
            "x103 = 10", "y103 = 8", "z103 = 6", "w103 = 0.4",
            # x104 携带 drag_mode 变量（模式三门控在 shader 内再确认）+ 粗格维度
            "x104 = $ssmtdrag_drag_enabled_testns", "y104 = 3", "z104 = 2", "w104 = 2",
        ):
            self.assertIn(expected, lines)
        # 反向断言：不得退回「一行多值」
        for bad in (
            "x101 = 1 0.002",
            "x102 = 0 0 0",
            "x103 = 10 8 6",
            "x104 = $ssmtdrag_drag_enabled_testns 3",
        ):
            self.assertNotIn(bad, text)

    def test_jiggle_section_without_collision_writes_zero_and_no_bindings(self):
        node, sections, comps = self._emit()  # 无 collision_grid
        cn = comps[0]["comp_name"]
        text = "\n".join(sections[f"[CustomShaderDragJiggle{cn}_testns]"])
        self.assertIn("x101 = 0", text)
        self.assertNotIn("cs-t76", text)
        self.assertNotIn("cs-t79", text)

    def test_component_resources_emit_collider_sections(self):
        node, sections, comps = self._emit_with_collision_grid()
        cn = comps[0]["comp_name"]
        for suffix in ("ColliderPoints", "ColliderCellsFine", "ColliderCellsCoarse", "ColliderVertexL0"):
            self.assertIn(f"[ResourceDrag{suffix}_{cn}_testns]", sections)

    def test_component_resources_no_collider_sections_by_default(self):
        node, sections, comps = self._emit()
        cn = comps[0]["comp_name"]
        for suffix in ("ColliderPoints", "ColliderCellsFine", "ColliderCellsCoarse", "ColliderVertexL0"):
            self.assertNotIn(f"[ResourceDrag{suffix}_{cn}_testns]", sections)

    # ---- HLSL 契约 ----

    def test_shader_collision_contract(self):
        shader = (REPO_ROOT / "Toolset" / "drag_interaction" / "rzm_jiggle_interaction.hlsl").read_text(encoding="utf-8")
        # 寄存器与 ini 发射的 cs-t76..t79 一致
        for reg in ("t76", "t77", "t78", "t79"):
            self.assertIn(f"register({reg})", shader)
        # 参数宏槽位与 ini 发射的 x101..x104 一致
        self.assertIn("#define COLLISION_PARAMS    IniParams[101]", shader)
        self.assertIn("#define COLLISION_BOX       IniParams[102]", shader)
        self.assertIn("#define COLLISION_GRID      IniParams[103]", shader)
        self.assertIn("#define COLLISION_META      IniParams[104]", shader)
        # 模式三门控（拉扯模式）在 shader 内显式再确认
        self.assertIn("COLLISION_META.x >= 2.0", shader)
        # 设计契约：触发用带符号表面距离（旧 bestD > margin 在稀疏点云下
        # 最近邻间距 ≫ margin，约束恒放行 → 防穿模从不触发）
        self.assertIn("float s1 = dot(p1 - bestQ, bestN);", shader)
        self.assertNotIn("if (bestD > margin)", shader)
        # 设计契约：逐顶点导出距离阈值（任何位移后距离不得低于 t0）
        self.assertIn("float threshold = l0q.w;", shader)
        self.assertIn("if (s1 >= threshold)", shader)
        # 无 while 循环（固定二分 + 固定上限扫描）；排除注释里的 "While" 字样
        self.assertNotIn("while (", shader)
        self.assertNotIn("while(", shader)

    # ---- 端到端烘焙 ----

    def test_write_collision_resources_end_to_end(self):
        import tempfile
        node = _make_node(self.mod, collision_enabled=True,
                          zone_objects=[self._collider_item(0, ["Thigh"])])
        verts = np.array([
            [-1, -1, -1], [1, -1, -1], [1, 1, -1], [-1, 1, -1],
            [-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1],
        ], dtype=np.float32)
        tris = np.array([
            [0, 1, 2], [0, 2, 3], [4, 6, 5], [4, 7, 6],
            [0, 4, 5], [0, 5, 1], [3, 2, 6], [3, 6, 7],
            [0, 3, 7], [0, 7, 4], [1, 5, 6], [1, 6, 2],
        ], dtype=np.int64)
        tri_part_names = np.array(["Thigh"] * 12, dtype=object)
        node._read_position_buf = lambda *a: verts
        node._read_component_triangles = lambda *a: (tris, tri_part_names)
        node._buffer_dir = lambda sections, comp: "Meshes"
        comp = {"vertex_count": 8, "base_name": "sample", "comp_name": "sample", "parts": []}

        with tempfile.TemporaryDirectory() as td:
            self.assertTrue(node._write_collision_resources(td, {}, comp, "testns"))
            p = Path(td) / "Meshes"
            self.assertTrue((p / "sampleColliderPoints.buf").exists())
            self.assertTrue((p / "sampleColliderCellsFine.buf").exists())
            self.assertTrue((p / "sampleColliderCellsCoarse.buf").exists())
            self.assertTrue((p / "sampleColliderVertexL0.buf").exists())
            self.assertIn("collision_grid", comp)
            # 点云交错布局：每点 2×float4 = 8 float
            pts = np.fromfile(p / "sampleColliderPoints.buf", dtype=np.float32)
            self.assertEqual(len(pts) % 8, 0)
            self.assertEqual(len(pts) // 8, 8)  # 8 碰撞体顶点未抽稀
            # L0：每顶点 2 float4，共 8 顶点
            l0 = np.fromfile(p / "sampleColliderVertexL0.buf", dtype=np.float32)
            self.assertEqual(len(l0), 8 * 2 * 4)

    def test_collision_l0_pads_zzmi_redirect_vertex_prefix(self):
        import tempfile

        node = _make_node(
            self.mod,
            collision_enabled=True,
            zone_objects=[self._collider_item(0, ["Thigh"])],
        )
        verts = np.array(
            [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
            dtype=np.float32,
        )
        triangles = np.array([[0, 1, 2]], dtype=np.int64)
        node._read_position_buf = lambda *args: verts
        node._read_component_triangles = lambda *args: (
            triangles,
            np.array(["Thigh"], dtype=object),
        )
        node._collider_l0_mask = lambda *args: np.array(
            [False, False, False, True, True, True], dtype=bool
        )
        node._buffer_dir = lambda sections, comp: "Meshes"
        comp = {
            "vertex_count": 6,
            "vertex_base": 3,
            "base_name": "sample",
            "comp_name": "sample",
            "parts": [],
        }

        with tempfile.TemporaryDirectory() as td:
            self.assertTrue(
                node._write_collision_resources(td, {}, comp, "testns")
            )
            l0 = np.fromfile(
                Path(td) / "Meshes" / "sampleColliderVertexL0.buf",
                dtype=np.float32,
            ).reshape(-1, 4)

        self.assertEqual(l0.shape, (12, 4))
        self.assertTrue(bool(np.all(l0[:6] == 0.0)))
        self.assertTrue(bool(np.all(l0[7::2, 3] == 1.0)))

    def test_write_collision_resources_skips_when_no_collider_hits(self):
        import tempfile
        node = _make_node(self.mod, collision_enabled=True,
                          zone_objects=[self._collider_item(0, ["Missing"])])
        verts = np.random.RandomState(4).rand(4, 3).astype(np.float32)
        tris = np.array([[0, 1, 2], [1, 2, 3]], dtype=np.int64)
        tri_part_names = np.array(["Other", "Other"], dtype=object)
        node._read_position_buf = lambda *a: verts
        node._read_component_triangles = lambda *a: (tris, tri_part_names)
        node._buffer_dir = lambda sections, comp: "Meshes"
        comp = {"vertex_count": 4, "base_name": "sample", "comp_name": "sample", "parts": []}

        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(node._write_collision_resources(td, {}, comp, "testns"))
            self.assertNotIn("collision_grid", comp)


if __name__ == "__main__":
    unittest.main()
