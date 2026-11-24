import importlib.util
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import _real_modules


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    module.__path__ = []
    module.__package__ = name
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_sk_dragdrive_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common"):
    _install_module(package_name)

# 真实 common 子模块按 fake 包前缀注册（空 __path__ 假包解析不了相对导入）
_real_modules.register_real_common_modules(f"{PKG}.common")
_safe_write = sys.modules[f"{PKG}.common.safe_write"]
_config = types.SimpleNamespace(_get_output_blueprint_tree=lambda: None)
_install_module(f"{PKG}.common.global_config", GlobalConfig=_config)

_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(PropertyGroup=object, Operator=object, UIList=object, Node=object),
    props=types.SimpleNamespace(
        StringProperty=lambda **_kw: None,
        BoolProperty=lambda **_kw: None,
        IntProperty=lambda **_kw: None,
        FloatProperty=lambda **_kw: None,
        EnumProperty=lambda **_kw: None,
        CollectionProperty=lambda **_kw: None,
        PointerProperty=lambda **_kw: None,
    ),
    data=types.SimpleNamespace(objects={}, texts=[], node_groups=types.SimpleNamespace(nodes=[])),
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module("bpy.props", **_fake_bpy.props.__dict__)
_install_module(
    "bpy.types",
    PropertyGroup=object,
    Operator=object,
    UIList=object,
    Node=object,
    NodeSocket=object,
)
_install_module("bpy.data", **_fake_bpy.data.__dict__)

_install_module(
    f"{PKG}.blueprint.node_postprocess_base",
    SSMTNode_PostProcess_Base=type(
        "_FakePostProcessBase",
        (object,),
        {
            "split_anim_driver_block_content": staticmethod(lambda content: ("", content)),
            "split_auto_appended_tail_content": staticmethod(lambda content: (content, "")),
            "_create_cumulative_backup": lambda self, ini_file_path, mod_export_path: None,
        },
    ),
)
_install_module(f"{PKG}.blueprint.direct_export", sync_shapekey_direct_mode=lambda self, ctx: None)
_install_module(f"{PKG}.blueprint.deform_chain")
_install_module(
    f"{PKG}.blueprint.export_helper",
    BlueprintExportHelper=types.SimpleNamespace(_collect_postprocess_nodes=lambda tree: tree.nodes),
)
_install_module(
    f"{PKG}.blueprint.variable_registry",
    allocate_shape_key_variable_name=lambda name, **_kw: f"Freq_{name}",
    mark_variable_name_used=lambda *_a, **_kw: None,
    normalize_variable_name=lambda value: str(value or "").strip(),
    cjk_to_ascii=lambda value: str(value or ""),
    is_pinyin_available=lambda **_kwargs: False,
    reset_pinyin_cache=lambda *_args, **_kwargs: None,
    shape_key_base_variable_name=lambda name: f"Freq_{name}",
    get_referenced_variable_names=lambda *_args, **_kwargs: set(),
)
_install_module(
    f"{PKG}.common.mod_path_compat",
    collect_base_position_resource_map=lambda *_a, **_kw: {},
    derive_shapekey_base_resource_name=lambda *a: "",
    derive_shapekey_freq_resource_name=lambda *a: "",
    derive_shapekey_merged_data_resource_name=lambda *a: "",
    derive_shapekey_merged_map_resource_name=lambda *a: "",
    derive_shapekey_slot_map_resource_name=lambda *a: "",
    derive_shapekey_slot_resource_name=lambda *a: "",
    derive_shapekey_vertex_entry_start_resource_name=lambda *a: "",
    derive_shapekey_vertex_entry_packed_resource_name=lambda *a: "",
    derive_shapekey_vertex_entry_freq_resource_name=lambda *a: "",
    derive_shapekey_weight_resource_name=lambda *a: "ResourceWeightStub",
    ensure_resource_alias_section=lambda *a, **_kw: "",
    resolve_hash_buffer_candidate=lambda *a, **_kw: None,
)
_install_module(
    f"{PKG}.common.object_prefix_helper",
    ObjectPrefixHelper=types.SimpleNamespace(resolve_source_object_name=lambda name: name),
)

_MODULE_PATH = Path(__file__).resolve().parents[1] / "blueprint" / "node_postprocess_shapekey.py"
_spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.node_postprocess_shapekey", _MODULE_PATH)
_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _module
_spec.loader.exec_module(_module)


def _read_text(path):
    with open(path, encoding="utf-8") as file_obj:
        return file_obj.read()


_write_text_if_changed = _safe_write.write_text_if_changed

_TEMPLATES = {
    "merged_delta": "shapekey_anim_packed_delta_v5_merged.hlsl",
    "merged_full": "shapekey_anim_packed_v5_merged.hlsl",
    "opt_delta": "shapekey_anim_packed_delta_v4_optimized.hlsl",
    "standard": "shapekey_anim_standard.hlsl",
}


def _make_node(zone_map, stage_map=None, dir_map=None):
    node = _module.SSMTNode_PostProcess_ShapeKey.__new__(_module.SSMTNode_PostProcess_ShapeKey)
    node.name = "SKNode"
    node.inputs = [types.SimpleNamespace(is_linked=False, links=[])]
    node.outputs = [types.SimpleNamespace(is_linked=False, links=[])]
    node.shapekey_variable_items = []
    node.shapekey_variable_index = 0
    node.id_data = types.SimpleNamespace(nodes=[])
    for name, zone in zone_map.items():
        item = _module.ShapeKeyVariableItem()
        item.shape_key_name = name
        item.assigned_variable_name = f"Freq_{name}"
        item.custom_variable_name = ""
        item.drag_zone_id = zone
        item.drag_click_stage = (stage_map or {}).get(name, 1)
        item.drag_dir_id = str((dir_map or {}).get(name, -1))
        node.shapekey_variable_items.append(item)
    return node


class ShapeKeyDragDriveTests(unittest.TestCase):
    ZONE_MAP = {"Breast_L": 2, "Breast_R": 3, "Hip": -1}

    def setUp(self):
        self.node = _make_node(self.ZONE_MAP)
        self.out_dir = tempfile.mkdtemp(prefix="sk_dragdrive_test_")

    def tearDown(self):
        shutil.rmtree(self.out_dir, ignore_errors=True)

    def _generate(self, template_name):
        src = os.path.abspath(os.path.join("Toolset", template_name))
        if not os.path.exists(src):
            self.skipTest(f"template missing: {src}")
        dest = os.path.join(self.out_dir, template_name)
        # 与生产路径一致：只在缺失时播种模板，随后**以模板为源**注入到目标。
        # 生产侧已不再用 shutil.copy2 —— 它会每轮重置目标 mtime，使 3DMigoto
        # 的编译缓存必然错配。
        if not os.path.exists(dest):
            _write_text_if_changed(dest, _read_text(src))
        self.node._update_shader_file(
            dest,
            {1: {"Breast_L": ["obj1"], "Breast_R": ["obj2"]}, 2: {"Hip": ["obj3"]}},
            True,
            True,
            ["Breast_L", "Breast_R", "Hip"],
            ["obj1", "obj2", "obj3"],
            use_optimized=True,
            merge_slot_files=(template_name in ("shapekey_anim_packed_delta_v5_merged.hlsl", "shapekey_anim_packed_v5_merged.hlsl")),
            drag_drive_enabled=True,
            drag_zone_ids=self.node._drag_drive_zone_ids(["Breast_L", "Breast_R", "Hip"]),
            source_path=src,
        )
        with open(dest, encoding="utf-8") as f:
            return f.read()

    def test_zone_ids_alignment(self):
        self.assertEqual(self.node._drag_drive_zone_ids(["Breast_L", "Breast_R", "Hip"]), [2, 3, -1])

    def test_stage_and_dir_helpers(self):
        node = _make_node(
            {"A": 0, "B": 0, "C": 1},
            stage_map={"A": 1, "B": 2, "C": 1},
            dir_map={"A": 0, "B": 2, "C": 3},
        )
        self.assertEqual(node._drag_drive_click_stages(["A", "B", "C"]), [1, 2, 1])
        self.assertEqual(node._drag_drive_dirs(["A", "B", "C"]), [0, 2, 3])

    def test_default_dir_is_no_direction_mapped_to_slot_4(self):
        node = _make_node({"A": 0, "B": 2})
        self.assertEqual(node._drag_drive_dirs(["A", "B"]), [4, 4])

    def test_regenerating_same_config_does_not_retouch_shader_file(self):
        """形态键着色器是**动态生成**的；同一配置重复导出必须不重写文件。

        3DMigoto 按 ``.hlsl`` 的 mtime 与 ``<stage>_5_0.<flags>.bin`` 配对
        （``CompareFileTime`` 必须精确相等）。若每次导出都重写，即使生成内容
        完全相同，mtime 也会被刷新、编译缓存整族失效，下次进游戏/F10 全量重编译。

        这里直接走生产路径（``shutil.copy2`` 模板 → ``_update_shader_file`` 注入）
        连跑两次，断言第二次不触碰文件。
        """
        template = "shapekey_anim_packed_delta_v5_merged.hlsl"
        target = os.path.join(self.out_dir, template)

        self._generate(template)
        self.assertTrue(os.path.isfile(target))
        with open(target, "rb") as file_obj:
            bytes_first = file_obj.read()
        mtime_first = os.path.getmtime(target)

        time.sleep(0.05)
        content_second = self._generate(template)
        mtime_second = os.path.getmtime(target)

        self.assertEqual(
            mtime_first, mtime_second,
            "同配置重复导出不得刷新着色器 mtime（否则 3DMigoto 编译缓存失效）",
        )
        with open(target, "rb") as file_obj:
            self.assertEqual(bytes_first, file_obj.read(), "磁盘字节不得变化")
        # 同时确认生成本身是确定性的（内容一致才有资格谈"不变就不写"）
        with open(target, encoding="utf-8") as file_obj:
            self.assertEqual(file_obj.read(), content_second)

    def test_negative_dir_maps_to_no_direction_slot(self):
        node = _make_node(
            {"A": 0, "B": 2},
            dir_map={"A": -1, "B": 0},
        )
        self.assertEqual(node._drag_drive_dirs(["A", "B"]), [4, 0])

    def test_find_drag_drive_node_reads_feature_switch(self):
        """S7 反扫谓词：优先读拖拽节点 _feature_skd()（四开关+档一消费方约束），
        旧节点/测试桩回退 enable_shapekey_drive。避免 F1 关闭后 t100/t101
        绑定指向未发射资源（phase2/n1 §5.2）。"""
        on = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_DragInteraction",
            _feature_skd=lambda: True,
        )
        off = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_DragInteraction",
            _feature_skd=lambda: False,
        )
        legacy = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_DragInteraction",
            enable_shapekey_drive=True,
        )
        legacy_off = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_DragInteraction",
            enable_shapekey_drive=False,
        )
        node = _make_node({})
        node.id_data = types.SimpleNamespace(nodes=[on])
        self.assertIs(node._find_drag_drive_node(), on)
        node.id_data = types.SimpleNamespace(nodes=[off])
        self.assertIsNone(node._find_drag_drive_node())
        node.id_data = types.SimpleNamespace(nodes=[legacy])
        self.assertIs(node._find_drag_drive_node(), legacy)
        node.id_data = types.SimpleNamespace(nodes=[legacy_off])
        self.assertIsNone(node._find_drag_drive_node())
        # 新旧混排：开启者胜出
        node.id_data = types.SimpleNamespace(nodes=[off, on])
        self.assertIs(node._find_drag_drive_node(), on)

    def test_find_drag_drive_node_uses_active_output_tree(self):
        drag = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_DragInteraction",
            enable_shapekey_drive=True,
        )
        muted = types.SimpleNamespace(**vars(drag), mute=True)
        node = _make_node({})
        node.id_data = types.SimpleNamespace(nodes=[])
        output_tree = types.SimpleNamespace(nodes=[muted, drag])

        with patch.object(_config, "_get_output_blueprint_tree", return_value=output_tree):
            self.assertIs(node._find_drag_drive_node(), drag)

    def test_non_directional_stages_and_directional_slot_layout(self):
        node = _make_node(
            {"A": 0, "B": 0, "C": 1},
            stage_map={"A": 1, "B": 2, "C": 1},
            dir_map={"A": -1, "B": -1, "C": 3},
        )
        # 模拟同树拖拽节点：区域0 无方向档位1/2（A/B），区域1 方向 C
        drag_node = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_DragInteraction",
            enable_shapekey_drive=True,
            _drag_drive_buffer_layout=lambda: (13, [0, 6], [2, 1]),
            _resolve_namespace=lambda ini: "ns",
        )
        node.id_data = types.SimpleNamespace(nodes=[drag_node])
        src = os.path.abspath(os.path.join("Toolset", "shapekey_anim_packed_delta_v5_merged.hlsl"))
        if not os.path.exists(src):
            self.skipTest("template missing")
        dest = os.path.join(self.out_dir, "multi.hlsl")
        shutil.copy2(src, dest)
        node._update_shader_file(
            dest,
            {1: {"A": ["obj1"], "B": ["obj1"]}, 2: {"C": ["obj2"]}},
            True,
            True,
            ["A", "B", "C"],
            ["obj1", "obj2"],
            use_optimized=True,
            merge_slot_files=True,
            drag_drive_enabled=True,
            drag_zone_ids=node._drag_drive_zone_ids(["A", "B", "C"]),
            drag_click_stages=node._drag_drive_click_stages(["A", "B", "C"]),
            drag_stage_count=node._drag_drive_stage_count(),
            drag_dirs=node._drag_drive_dirs(["A", "B", "C"]),
        )
        with open(dest, encoding="utf-8") as f:
            content = f.read()
        # 区域0 无方向档位 A=1、B=2；区域1 方向 C=3
        # 区域0 段 = 4+2 = 6 槽；区域1 基址 = 6；C 槽 = 6+3 = 9
        self.assertIn("static const uint SHAPEKEY_ND_STAGE_IDS[3] = { 1, 2, 4294967295 };", content)
        self.assertIn("static const uint SHAPEKEY_SLOT_IDS[3] = { 4, 5, 9 };", content)
        # A/B 无方向槽读取带档位门控；C 方向槽忽略档位
        self.assertIn("// A (zone 0, slot 4)", content)
        self.assertIn("// B (zone 0, slot 5)", content)
        self.assertIn("// C (zone 1, slot 9)", content)

    def test_merged_optimized_generates_drive_read(self):
        content = self._generate("shapekey_anim_packed_delta_v5_merged.hlsl")
        self.assertIn("Buffer<float> ShapeKeyDrive : register(t100);", content)
        self.assertIn("Buffer<uint> ShapeKeyClickCount : register(t101);", content)
        self.assertIn("SHAPEKEY_ZONE_IDS", content)
        self.assertIn("SHAPEKEY_SLOT_IDS", content)
        self.assertIn("sk_nd_stage_slot0 == 0xFFFFFFFFu || ShapeKeyClickCount[sk_zone_slot0] == sk_nd_stage_slot0", content)
        self.assertIn("anim_weight_slot0 = ShapeKeyDrive[sk_slot_slot0];", content)
        self.assertIn("#define FREQ1 (SHAPEKEY_ND_STAGE_IDS[0] == 0xFFFFFFFFu || ShapeKeyClickCount[SHAPEKEY_ZONE_IDS[0]] == SHAPEKEY_ND_STAGE_IDS[0]", content)
        # 未绑定形态键保持变量回退
        self.assertIn("#define FREQ3 ShapeKeyWeight[2]", content)
        # 未绑定守卫：不会访问 ShapeKeyDrive[-1]
        self.assertIn("0xFFFFFFFFu", content)

    def test_optimized_non_merged_generates_drive_read(self):
        content = self._generate("shapekey_anim_packed_delta_v4_optimized.hlsl")
        self.assertIn("register(t100)", content)
        self.assertIn("sk_nd_stage_slot0 == 0xFFFFFFFFu || ShapeKeyClickCount[sk_zone_slot0] == sk_nd_stage_slot0", content)
        self.assertIn("anim_weight_slot0 = ShapeKeyDrive[sk_slot_slot0];", content)

    def test_all_templates_generate_drive_binding(self):
        for template_name in _TEMPLATES.values():
            content = self._generate(template_name)
            self.assertIn("register(t100)", content, template_name)
            self.assertIn("SHAPEKEY_SLOT_IDS", content, template_name)
            self.assertIn("uint sk_slot_slot0 = SHAPEKEY_SLOT_IDS[freq_idx_slot0];", content, template_name)
            self.assertIn("ShapeKeyDrive[sk_slot_slot0]", content, template_name)

    def test_disabled_reads_weight_from_dedicated_buffer(self):
        src = os.path.abspath(os.path.join("Toolset", "shapekey_anim_packed_delta_v5_merged.hlsl"))
        if not os.path.exists(src):
            self.skipTest("template missing")
        dest = os.path.join(self.out_dir, "disabled.hlsl")
        shutil.copy2(src, dest)
        self.node._update_shader_file(
            dest,
            {1: {"Breast_L": ["obj1"]}},
            True,
            True,
            ["Breast_L"],
            ["obj1"],
            use_optimized=True,
            merge_slot_files=True,
            drag_drive_enabled=False,
        )
        with open(dest, encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("ShapeKeyDrive", content)
        self.assertIn("Buffer<float> ShapeKeyWeight : register(t102);", content)
        self.assertIn("ShapeKeyWeight[freq_idx_slot0]", content)

    def test_no_zone_bound_still_emits_full_length_lookup_arrays(self):
        """开了拖拽驱动但一个区域都没绑：三张查找表必须满长度，不得发 [1] 占位。

        回归：旧实现在「无任何区域绑定」时发
        ``static const uint SHAPEKEY_ZONE_IDS[1] = { 0xFFFFFFFFu };``
        而取值逻辑仍按 ``freq_idx``（0..len(unique_names)-1）索引三张表 →
        HLSL 不做边界检查 → 越界读常量缓冲，读到垃圾 zone/slot →
        ``anim_weight`` 被 ``ShapeKeyDrive[垃圾]`` 顶掉，表现为面板变量
        拉不动对应形态键（联动"看起来坏了"）。
        """
        node = _make_node({"A": -1, "B": -1, "C": -1})
        src = os.path.abspath(os.path.join("Toolset", "shapekey_anim_packed_delta_v5_merged.hlsl"))
        if not os.path.exists(src):
            self.skipTest("template missing")
        dest = os.path.join(self.out_dir, "nozone.hlsl")
        node._update_shader_file(
            dest,
            {1: {"A": ["obj1"], "B": ["obj1"], "C": ["obj1"]}},
            True,
            True,
            ["A", "B", "C"],
            ["obj1"],
            use_optimized=True,
            merge_slot_files=True,
            drag_drive_enabled=True,
            drag_zone_ids=node._drag_drive_zone_ids(["A", "B", "C"]),
            drag_click_stages=node._drag_drive_click_stages(["A", "B", "C"]),
            drag_dirs=node._drag_drive_dirs(["A", "B", "C"]),
            source_path=src,
        )
        with open(dest, encoding="utf-8") as f:
            content = f.read()
        sentinel = "0xFFFFFFFFu, 0xFFFFFFFFu, 0xFFFFFFFFu"
        self.assertIn(f"static const uint SHAPEKEY_ZONE_IDS[3] = {{ {sentinel} }};", content)
        # 档位/槽位表沿用既有十进制写法（与既有测试基线一致）
        decimal_sentinel = "4294967295, 4294967295, 4294967295"
        self.assertIn(f"static const uint SHAPEKEY_ND_STAGE_IDS[3] = {{ {decimal_sentinel} }};", content)
        self.assertIn(f"static const uint SHAPEKEY_SLOT_IDS[3] = {{ {decimal_sentinel} }};", content)
        self.assertNotIn("SHAPEKEY_ZONE_IDS[1]", content)
        self.assertNotIn("SHAPEKEY_SLOT_IDS[1]", content)
        # 未绑定 → FREQ 走变量回退（面板变量直控形态键）
        self.assertIn("#define FREQ1 ShapeKeyWeight[0]", content)
        self.assertIn("#define FREQ3 ShapeKeyWeight[2]", content)

    def test_drag_drive_fields_hidden_when_toggle_off(self):
        node = _make_node({"A": 2})
        calls = []

        class _FakeRow:
            def row(self, align=False):
                return self

            def column(self, align=False):
                return self

            def label(self, text="", icon=""):
                pass

            def prop(self, data, prop, **kwargs):
                calls.append((prop, kwargs.get("text", "")))

        fake_row = _FakeRow()

        class _FakeLayout:
            def row(self, align=False):
                return fake_row

            def column(self, align=False):
                return self

            def label(self, text="", icon=""):
                pass

        ulist = _module.SSMT_UL_ShapeKeyVariableMappings()
        ulist.layout_type = "DEFAULT"

        # 开关关闭：不绘制区域/档位/方向
        node.drag_drive_enabled = False
        ulist.draw_item(
            None, _FakeLayout(), node, node.shapekey_variable_items[0],
            "", None, "", 0,
        )
        self.assertNotIn(("drag_zone_id", "区域"), calls)
        self.assertNotIn(("drag_click_stage", "档位"), calls)
        self.assertNotIn(("drag_dir_id", "方向"), calls)

        # 开关打开：绘制区域/档位/方向
        calls.clear()
        node.drag_drive_enabled = True
        ulist.draw_item(
            None, _FakeLayout(), node, node.shapekey_variable_items[0],
            "", None, "", 0,
        )
        self.assertIn(("drag_zone_id", "区域"), calls)
        self.assertIn(("drag_click_stage", "档位"), calls)
        self.assertIn(("drag_dir_id", "方向"), calls)


if __name__ == "__main__":
    unittest.main()
